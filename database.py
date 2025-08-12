# database.py - VERSIÓN CON MÓDULO DE INTELIGENCIA

import sqlite3
import json

DATABASE_FILE = '/tmp/facturas.db'

def init_db():
    conn = sqlite3.connect(DATABASE_FILE); cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS facturas (id INTEGER PRIMARY KEY, emisor TEXT, cif TEXT, fecha TEXT, total REAL, base_imponible REAL, impuestos_json TEXT, ia_model TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    cursor.execute('CREATE TABLE IF NOT EXISTS conceptos (id INTEGER PRIMARY KEY, factura_id INTEGER, descripcion TEXT, cantidad REAL, precio_unitario REAL, FOREIGN KEY (factura_id) REFERENCES facturas (id))')
    conn.commit(); conn.close(); print("Base de datos inicializada en", DATABASE_FILE)

def add_invoice(invoice_data: dict, ia_model: str):
    try:
        conn = sqlite3.connect(DATABASE_FILE); cursor = conn.cursor()
        cursor.execute('INSERT INTO facturas (emisor, cif, fecha, total, base_imponible, impuestos_json, ia_model) VALUES (?, ?, ?, ?, ?, ?, ?)', (
            invoice_data.get('emisor'), invoice_data.get('cif'), invoice_data.get('fecha'), invoice_data.get('total'),
            invoice_data.get('base_imponible'), json.dumps(invoice_data.get('impuestos')), ia_model
        ))
        factura_id = cursor.lastrowid
        conceptos_list = invoice_data.get('conceptos', [])
        if conceptos_list and isinstance(conceptos_list, list):
            for concepto in conceptos_list:
                cursor.execute('INSERT INTO conceptos (factura_id, descripcion, cantidad, precio_unitario) VALUES (?, ?, ?, ?)', (
                    factura_id, concepto.get('descripcion'), concepto.get('cantidad'), concepto.get('precio_unitario')
                ))
        conn.commit(); conn.close(); return factura_id
    except Exception as e:
        print(f"Error al guardar en DB: {e}"); return None

def get_all_invoices():
    conn = sqlite3.connect(DATABASE_FILE); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
    cursor.execute('SELECT id, emisor, fecha, total FROM facturas ORDER BY fecha DESC')
    invoices = [dict(row) for row in cursor.fetchall()]; conn.close(); return invoices

def get_invoice_details(invoice_id: int):
    conn = sqlite3.connect(DATABASE_FILE); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
    cursor.execute('SELECT * FROM facturas WHERE id = ?', (invoice_id,)); invoice = cursor.fetchone()
    if not invoice: return None
    cursor.execute('SELECT descripcion, cantidad, precio_unitario FROM conceptos WHERE factura_id = ?', (invoice_id,)); conceptos = [dict(row) for row in cursor.fetchall()]
    invoice_details = dict(invoice); invoice_details['conceptos'] = conceptos
    try: invoice_details['impuestos'] = json.loads(invoice_details.get('impuestos_json', '{}'))
    except (json.JSONDecodeError, TypeError): invoice_details['impuestos'] = {}
    if 'impuestos_json' in invoice_details: del invoice_details['impuestos_json']
    conn.close(); return invoice_details

# --- >>> NUEVA FUNCIÓN PARA DAR CONTEXTO A GEMINI <<< ---
def get_all_invoices_with_details():
    """
    Obtiene todas las facturas con sus conceptos para proporcionarlas como contexto a la IA.
    """
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM facturas ORDER BY fecha DESC')
    facturas = cursor.fetchall()
    
    invoices_list = []
    for factura_row in facturas:
        invoice_details = dict(factura_row)
        factura_id = invoice_details['id']
        
        cursor.execute('SELECT descripcion, cantidad, precio_unitario FROM conceptos WHERE factura_id = ?', (factura_id,))
        conceptos = [dict(row) for row in cursor.fetchall()]
        invoice_details['conceptos'] = conceptos
        
        try:
            invoice_details['impuestos'] = json.loads(invoice_details.get('impuestos_json', '{}'))
        except (json.JSONDecodeError, TypeError):
            invoice_details['impuestos'] = {}
        if 'impuestos_json' in invoice_details:
            del invoice_details['impuestos_json']
            
        invoices_list.append(invoice_details)
        
    conn.close()
    return invoices_list