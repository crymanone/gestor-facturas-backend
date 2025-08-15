# database.py - VERSIÓN CON MULTI-USUARIO Y COLUMNA user_id

import os
import psycopg2
import psycopg2.extras
import json
import uuid

def get_db_connection():
    # ... (sin cambios)
    conn_string = os.environ.get('DATABASE_URL');
    if not conn_string: raise ValueError("No DATABASE_URL")
    return psycopg2.connect(conn_string, connect_timeout=10)

def init_db(): # ... (sin cambios)
    pass 

def to_float(value): # ... (sin cambios)
    if value is None: return 0.0
    try: return float(value)
    except (ValueError, TypeError): return 0.0

def add_invoice(invoice_data: dict, ia_model: str, user_id: str):
    sql_factura = """
    INSERT INTO facturas (emisor, cif, fecha, total, base_imponible, impuestos_json, ia_model, user_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
    """
    sql_concepto = "INSERT INTO conceptos (factura_id, descripcion, cantidad, precio_unitario) VALUES (%s, %s, %s, %s);"
    conn = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        impuestos_str = json.dumps(invoice_data.get('impuestos')) if isinstance(invoice_data.get('impuestos'), dict) else None
        cur.execute(sql_factura, (
            invoice_data.get('emisor'), invoice_data.get('cif'), invoice_data.get('fecha'),
            to_float(invoice_data.get('total')), to_float(invoice_data.get('base_imponible')),
            impuestos_str, ia_model, user_id
        ))
        factura_id = cur.fetchone()[0]
        conceptos = invoice_data.get('conceptos', [])
        if conceptos and isinstance(conceptos, list):
            for c in conceptos:
                cur.execute(sql_concepto, (factura_id, c.get('descripcion'), to_float(c.get('cantidad')), to_float(c.get('precio_unitario'))))
        conn.commit(); cur.close(); return factura_id
    except (Exception, psycopg2.DatabaseError) as error:
        print(f"Error DB: {error}");
        if conn: conn.rollback()
        return None
    finally:
        if conn: conn.close()

def create_pdf_job(pdf_data, user_id: str): # Añadimos user_id para asociar el trabajo
    job_id = str(uuid.uuid4())
    # Necesitaremos una columna para user_id en la cola también
    # Por ahora, para simplificar, lo guardaremos en el result_json, pero idealmente iría en una columna
    # Lo más fácil es meterlo como un campo especial en el result_json al crearlo
    metadata = {'user_id': user_id}
    sql = "INSERT INTO pdf_processing_queue (id, status, pdf_data, result_json) VALUES (%s, 'pending', %s, %s);"
    conn = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute(sql, (job_id, psycopg2.Binary(pdf_data), json.dumps(metadata)))
        conn.commit(); cur.close(); return job_id
    finally:
        if conn: conn.close()
 
def get_job_status(job_id): # ... (sin cambios, ya que el job_id es único)
    sql = "SELECT status, result_json, error_message FROM pdf_processing_queue WHERE id = %s;"
    conn = None; # ... resto igual

def get_pending_pdf_job(): # ... (sin cambios)
     sql = "UPDATE pdf_processing_queue SET status = 'processing' WHERE id = (SELECT id FROM pdf_processing_queue WHERE status = 'pending' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id, pdf_data, result_json;"
     conn = None; # ... resto igual

def update_job_as_completed(job_id, result_json): # ... (sin cambios)
     sql = "UPDATE pdf_processing_queue SET status = 'completed', result_json = %s, pdf_data = NULL WHERE id = %s;"
     conn = None; # ... resto igual

def update_job_as_failed(job_id, error_message): # ... (sin cambios)
     sql = "UPDATE pdf_processing_queue SET status = 'failed', error_message = %s, pdf_data = NULL WHERE id = %s;"
     conn = None; # ... resto igual

def get_all_invoices(user_id: str):
    conn = None 
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute('SELECT id, emisor, fecha, total FROM facturas WHERE user_id = %s ORDER BY fecha DESC, id DESC', (user_id,))
        invoices = [dict(row) for row in cur.fetchall()]
        cur.close(); return invoices
    finally:
        if conn: conn.close()

def get_invoice_details(invoice_id: int, user_id: str):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute('SELECT * FROM facturas WHERE id = %s AND user_id = %s', (invoice_id, user_id))
        invoice = cur.fetchone()
        if not invoice: return None
        # ... resto igual ... 
        cur.execute('SELECT descripcion, cantidad, precio_unitario FROM conceptos WHERE factura_id = %s', (invoice_id,))
        conceptos = [dict(row) for row in cur.fetchall()]
        invoice_details = dict(invoice)
        invoice_details['conceptos'] = conceptos; invoice_details['impuestos'] = invoice_details.get('impuestos_json') or {}
        if 'impuestos_json' in invoice_details: del invoice_details['impuestos_json']
        cur.close(); return invoice_details
    finally:
        if conn: conn.close()

def get_all_invoices_with_details(user_id: str):
    conn = None; # ... similar a get_all_invoices, pero con más datos
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute('SELECT * FROM facturas WHERE user_id = %s ORDER BY fecha DESC, id DESC', (user_id,))
        facturas = cur.fetchall()
        invoices_list = []
        for f in facturas:
            details = dict(f); factura_id = details['id']
            cur.execute('SELECT * FROM conceptos WHERE factura_id = %s', (factura_id,))
            details['conceptos'] = [dict(row) for row in cur.fetchall()]
            invoices_list.append(details)
        cur.close(); return invoices_list
    finally:
        if conn: conn.close()

def search_invoices(user_id: str, text_query=None, date_from=None, date_to=None):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        query = "SELECT DISTINCT f.* FROM facturas f LEFT JOIN conceptos c ON f.id = c.factura_id WHERE f.user_id = %s"
        params = [user_id]
        if text_query:
            query += " AND (LOWER(f.emisor) LIKE %s OR LOWER(c.descripcion) LIKE %s)"
            params.extend([f'%{text_query.lower()}%', f'%{text_query.lower()}%'])
        # ... resto igual ...
        cur.execute(query, tuple(params))
        return [dict(row) for row in cur.fetchall()]
    finally:
        if conn: conn.close()

def delete_invoice(invoice_id: int, user_id: str):
    conn = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        # Clave: solo borra si el user_id coincide. El ON DELETE CASCADE se encarga de los conceptos.
        cur.execute("DELETE FROM facturas WHERE id = %s AND user_id = %s RETURNING id", (invoice_id, user_id))
        was_deleted = cur.fetchone() is not None
        conn.commit(); cur.close(); return was_deleted
    except (Exception, psycopg2.DatabaseError) as error:
        print(f"Error borrando: {error}");
        if conn: conn.rollback()
        return False
    finally:
        if conn: conn.close()