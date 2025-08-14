# database.py - VERSIÓN FINAL CON SUPABASE (PostgreSQL)

import os
import psycopg2
import psycopg2.extras # Para obtener resultados como diccionarios
import json

def get_db_connection():
    """Crea una conexión a la base de datos de Supabase."""
    conn_string = os.environ.get('DATABASE_URL')
    if not conn_string:
        raise ValueError("No se encontró la variable de entorno DATABASE_URL")
    conn = psycopg2.connect(conn_string)
    return conn

def init_db():
    """
    Verifica la conexión. Las tablas se crean en la interfaz de Supabase.
    """
    try:
        conn = get_db_connection()
        print("Conexión con Supabase establecida correctamente.")
        conn.close()
    except Exception as e:
        print(f"Error al conectar con Supabase: {e}")

def add_invoice(invoice_data: dict, ia_model: str):
    sql_factura = """
    INSERT INTO facturas (emisor, cif, fecha, total, base_imponible, impuestos_json, ia_model)
    VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id;
    """
    sql_concepto = """
    INSERT INTO conceptos (factura_id, descripcion, cantidad, precio_unitario)
    VALUES (%s, %s, %s, %s);
    """
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        total = float(invoice_data.get('total') or 0.0)
        base_imponible = float(invoice_data.get('base_imponible') or 0.0)
        
        cur.execute(sql_factura, (
            invoice_data.get('emisor'), invoice_data.get('cif'), invoice_data.get('fecha'),
            total, base_imponible, json.dumps(invoice_data.get('impuestos')), ia_model
        ))
        
        factura_id = cur.fetchone()[0]
        
        conceptos_list = invoice_data.get('conceptos', [])
        if conceptos_list and isinstance(conceptos_list, list):
            for concepto in conceptos_list:
                cantidad = float(concepto.get('cantidad') or 0.0)
                precio_unitario = float(concepto.get('precio_unitario') or 0.0)
                cur.execute(sql_concepto, (
                    factura_id, concepto.get('descripcion'), cantidad, precio_unitario
                ))
        
        conn.commit()
        cur.close()
        return factura_id
    except (Exception, psycopg2.DatabaseError) as error:
        print(f"Error en la transacción de la base de datos: {error}")
        if conn: conn.rollback()
        return None
    finally:
        if conn: conn.close()

def get_all_invoices():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute('SELECT id, emisor, fecha, total FROM facturas ORDER BY fecha DESC, id DESC')
    invoices = [dict(row) for row in cur.fetchall()]
    cur.close()
    conn.close()
    return invoices

def get_invoice_details(invoice_id: int):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cur.execute('SELECT * FROM facturas WHERE id = %s', (invoice_id,));
    invoice = cur.fetchone()
    if not invoice: return None
    
    cur.execute('SELECT descripcion, cantidad, precio_unitario FROM conceptos WHERE factura_id = %s', (invoice_id,))
    conceptos = [dict(row) for row in cur.fetchall()]
    
    invoice_details = dict(invoice)
    invoice_details['conceptos'] = conceptos
    
    # El tipo jsonb de postgres ya devuelve un dict, no hace falta json.loads
    invoice_details['impuestos'] = invoice_details.get('impuestos_json') or {}
    if 'impuestos_json' in invoice_details: del invoice_details['impuestos_json']

    cur.close()
    conn.close()
    return invoice_details

def get_all_invoices_with_details():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cur.execute('SELECT * FROM facturas ORDER BY fecha DESC, id DESC')
    facturas = cur.fetchall()
    
    invoices_list = []
    for factura_row in facturas:
        invoice_details = dict(factura_row)
        factura_id = invoice_details['id']
        
        cur.execute('SELECT descripcion, cantidad, precio_unitario FROM conceptos WHERE factura_id = %s', (factura_id,))
        conceptos = [dict(row) for row in cur.fetchall()]
        invoice_details['conceptos'] = conceptos
        
        invoice_details['impuestos'] = invoice_details.get('impuestos_json') or {}
        if 'impuestos_json' in invoice_details: del invoice_details['impuestos_json']
            
        invoices_list.append(invoice_details)
        
    cur.close()
    conn.close()
    return invoices_list

def search_invoices(text_query=None, date_from=None, date_to=None):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    query = """
    SELECT DISTINCT f.id, f.emisor, f.fecha, f.total 
    FROM facturas f
    LEFT JOIN conceptos c ON f.id = c.factura_id
    WHERE 1=1
    """
    params = []

    if text_query:
        query += " AND (LOWER(f.emisor) LIKE %s OR LOWER(c.descripcion) LIKE %s)"
        params.extend([f'%{text_query.lower()}%', f'%{text_query.lower()}%'])

    # Para PostgreSQL, es mejor castear a fecha
    if date_from:
        query += " AND TO_DATE(f.fecha, 'DD/MM/YYYY') >= %s"
        params.append(date_from) # Espera formato AAAA-MM-DD
    
    if date_to:
        query += " AND TO_DATE(f.fecha, 'DD/MM/YYYY') <= %s"
        params.append(date_to) # Espera formato AAAA-MM-DD

    query += " ORDER BY TO_DATE(f.fecha, 'DD/MM/YYYY') DESC, f.id DESC"
    
    cur.execute(query, tuple(params))
    results = [dict(row) for row in cur.fetchall()]
    cur.close()
    conn.close()
    return results