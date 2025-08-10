# database.py - VERSIÓN COMPLETA CON LÓGICA DE ORDENACIÓN

import sqlite3
import json

DATABASE_FILE = 'facturas.db'

def init_db():
    """Crea las tablas de la base de datos si no existen."""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    # Tabla principal para las facturas
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS facturas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emisor TEXT,
        cif TEXT,
        fecha TEXT,
        total REAL,
        base_imponible REAL,
        impuestos_json TEXT,
        ia_model TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # Tabla para los conceptos de cada factura
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS conceptos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        factura_id INTEGER,
        descripcion TEXT,
        cantidad REAL,
        precio_unitario REAL,
        FOREIGN KEY (factura_id) REFERENCES facturas (id)
    )
    ''')
    
    conn.commit()
    conn.close()
    print("Base de datos inicializada.")

def add_invoice(invoice_data: dict, ia_model: str):
    """Añade una nueva factura y sus conceptos a la base de datos."""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        cursor.execute('''
        INSERT INTO facturas (emisor, cif, fecha, total, base_imponible, impuestos_json, ia_model)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            invoice_data.get('emisor'),
            invoice_data.get('cif'),
            invoice_data.get('fecha'),
            invoice_data.get('total'),
            invoice_data.get('base_imponible'),
            json.dumps(invoice_data.get('impuestos')),
            ia_model
        ))
        
        factura_id = cursor.lastrowid
        
        conceptos_list = invoice_data.get('conceptos', [])
        if conceptos_list:
            for concepto in conceptos_list:
                cursor.execute('''
                INSERT INTO conceptos (factura_id, descripcion, cantidad, precio_unitario)
                VALUES (?, ?, ?, ?)
                ''', (
                    factura_id,
                    concepto.get('descripcion'),
                    concepto.get('cantidad'),
                    concepto.get('precio_unitario')
                ))

        conn.commit()
        conn.close()
        print(f"Factura con ID {factura_id} guardada en la base de datos.")
        return factura_id
    except Exception as e:
        print(f"Error al guardar en la base de datos: {e}")
        return None

# --- >>> FUNCIÓN MODIFICADA <<< ---
def get_all_invoices(sort_by='fecha', order='DESC'):
    """
    Recupera un resumen de todas las facturas, con opción de ordenación.
    :param sort_by: Columna por la que ordenar ('emisor', 'fecha', 'total').
    :param order: Orden ('ASC' para ascendente, 'DESC' para descendente).
    """
    allowed_sort_columns = ['emisor', 'fecha', 'total']
    if sort_by not in allowed_sort_columns:
        sort_by = 'fecha'
    
    if order.upper() not in ['ASC', 'DESC']:
        order = 'DESC'

    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row 
    cursor = conn.cursor()
    
    query = f'SELECT id, emisor, fecha, total FROM facturas ORDER BY {sort_by} {order}'
    print(f"Ejecutando consulta: {query}")
    cursor.execute(query)
    
    invoices = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    return invoices

def get_invoice_details(invoice_id: int):
    """Recupera todos los detalles de una factura específica, incluyendo sus conceptos."""
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute('SELECT * FROM facturas WHERE id = ?', (invoice_id,))
    invoice = cursor.fetchone()

    if not invoice:
        return None

    cursor.execute('SELECT descripcion, cantidad, precio_unitario FROM conceptos WHERE factura_id = ?', (invoice_id,))
    conceptos = [dict(row) for row in cursor.fetchall()]

    invoice_details = dict(invoice)
    invoice_details['conceptos'] = conceptos
    
    try:
        invoice_details['impuestos'] = json.loads(invoice_details['impuestos_json'])
    except:
        invoice_details['impuestos'] = {}
    del invoice_details['impuestos_json']
    
    conn.close()
    return invoice_details