# database.py - VERSIÓN COMPLETA CON BÚSQUEDA INTELIGENTE

import sqlite3
import json
from datetime import datetime

# Vercel solo permite escribir en /tmp
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
        if conceptos_list:
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
    try: invoice_details['impuestos'] = json.loads(invoice_details['impuestos_json'])
    except: invoice_details['impuestos'] = {}; del invoice_details['impuestos_json']
    conn.close(); return invoice_details

# --- >>> NUEVAS FUNCIONES PARA EL ASISTENTE DE IA <<< ---
def search_invoices(filters: dict):
    conn = sqlite3.connect(DATABASE_FILE); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
    query = 'SELECT id, emisor, fecha, total FROM facturas WHERE 1=1'; params = []
    
    if filters.get('emisor'):
        query += ' AND lower(emisor) LIKE ?'; params.append(f"%{filters['emisor'].lower()}%")
        
    if filters.get('mes'):
        # Extrae el mes de la fecha guardada como texto (YYYY-MM-DD o DD/MM/YYYY)
        query += " AND substr(fecha, instr(fecha, '/') + 1, 2) = ?"; params.append(str(filters['mes']).zfill(2))
    
    query += " ORDER BY fecha DESC"
    cursor.execute(query, params)
    invoices = [dict(row) for row in cursor.fetchall()]; conn.close(); return invoices

def get_monthly_summary():
    conn = sqlite3.connect(DATABASE_FILE); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
    query = "SELECT substr(fecha, -4) || '-' || substr(fecha, -7, 2) as mes, SUM(total) as gasto_total FROM facturas GROUP BY mes ORDER BY mes DESC LIMIT 6;"
    cursor.execute(query); summary = [dict(row) for row in cursor.fetchall()]; conn.close(); return summary