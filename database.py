# database.py - VERSIÓN FINAL CON MIGRACIÓN DE BASE DE DATOS
import os
import psycopg2
import psycopg2.extras
import json
import uuid

def get_db_connection():
    conn_string = os.environ.get('DATABASE_URL')
    if not conn_string:
        raise ValueError("No se encontró la variable de entorno DATABASE_URL")
    conn = psycopg2.connect(conn_string, connect_timeout=10)
    return conn

def init_db():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # --- CREACIÓN DE TABLAS (SI NO EXISTEN) ---
        cur.execute('CREATE TABLE IF NOT EXISTS facturas (id BIGSERIAL PRIMARY KEY, emisor TEXT, cif TEXT, fecha TEXT, total REAL, base_imponible REAL, impuestos_json JSONB, ia_model TEXT, user_id TEXT, created_at TIMESTAMPTZ DEFAULT NOW());')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_facturas_user_id ON facturas(user_id);')
        cur.execute('CREATE TABLE IF NOT EXISTS conceptos (id BIGSERIAL PRIMARY KEY, factura_id BIGINT REFERENCES facturas(id) ON DELETE CASCADE, descripcion TEXT, cantidad REAL, precio_unitario REAL);')
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pdf_processing_queue (
                id UUID PRIMARY KEY,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                status TEXT NOT NULL,
                pdf_data BYTEA,
                result_json JSONB,
                error_message TEXT
            );
        """)
        
        # --- ARREGLO DEFINITIVO: MIGRACIÓN DE LA BASE DE DATOS ---
        # Comprueba si la columna 'user_id' existe en la tabla de trabajos.
        cur.execute("""
            SELECT 1 FROM information_schema.columns 
            WHERE table_name='pdf_processing_queue' AND column_name='user_id'
        """)
        column_exists = cur.fetchone()
        
        # Si la columna NO existe, la añade.
        if not column_exists:
            print("Detectada versión antigua de la tabla 'pdf_processing_queue'. Añadiendo columna 'user_id'...")
            cur.execute('ALTER TABLE pdf_processing_queue ADD COLUMN user_id TEXT;')
            print("Columna 'user_id' añadida correctamente.")
        # --- FIN DEL ARREGLO ---
            
        conn.commit()
        cur.close()
        print("Base de datos y tablas listas.")
    except Exception as e:
        print(f"Error al inicializar la base de datos: {e}")
    finally:
        if conn: conn.close()

def to_float(value):
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
        conceptos_list = invoice_data.get('conceptos', [])
        if conceptos_list and isinstance(conceptos_list, list):
            for concepto in conceptos_list:
                cur.execute(sql_concepto, (factura_id, concepto.get('descripcion'), to_float(concepto.get('cantidad')), to_float(concepto.get('precio_unitario'))))
        conn.commit(); cur.close(); return factura_id
    except (Exception, psycopg2.DatabaseError) as error:
        print(f"Error DB en add_invoice: {error}");
        if conn: conn.rollback()
        return None
    finally:
        if conn: conn.close()

def create_pdf_job(pdf_data, user_id: str):
    job_id = str(uuid.uuid4())
    sql = "INSERT INTO pdf_processing_queue (id, status, pdf_data, user_id) VALUES (%s, 'pending', %s, %s);"
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(sql, (job_id, psycopg2.Binary(pdf_data), user_id))
        conn.commit()
        cur.close()
        return job_id
    finally:
        if conn: conn.close()

def get_job_status(job_id):
    sql = "SELECT status, result_json, error_message FROM pdf_processing_queue WHERE id = %s;"
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(sql, (uuid.UUID(job_id),))
        job = cur.fetchone()
        cur.close()
        return dict(job) if job else None
    finally:
        if conn: conn.close()

def get_pending_pdf_job():
    sql = "UPDATE pdf_processing_queue SET status = 'processing' WHERE id = (SELECT id FROM pdf_processing_queue WHERE status = 'pending' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id, pdf_data, user_id;"
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(sql)
        job = cur.fetchone()
        conn.commit()
        cur.close()
        return dict(job) if job else None
    finally:
        if conn: conn.close()

def update_job_as_completed(job_id, result_json):
    sql = "UPDATE pdf_processing_queue SET status = 'completed', result_json = %s, pdf_data = NULL WHERE id = %s;"
    conn = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute(sql, (json.dumps(result_json), job_id))
        conn.commit(); cur.close()
    finally:
        if conn: conn.close()

def update_job_as_failed(job_id, error_message):
    sql = "UPDATE pdf_processing_queue SET status = 'failed', error_message = %s, pdf_data = NULL WHERE id = %s;"
    conn = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute(sql, (error_message, job_id))
        conn.commit(); cur.close()
    finally:
        if conn: conn.close()

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
        cur.execute('SELECT descripcion, cantidad, precio_unitario FROM conceptos WHERE factura_id = %s', (invoice_id,))
        conceptos = [dict(row) for row in cur.fetchall()]
        invoice_details = dict(invoice)
        invoice_details['conceptos'] = conceptos; invoice_details['impuestos'] = json.loads(invoice_details.get('impuestos_json') or '{}')
        if 'impuestos_json' in invoice_details: del invoice_details['impuestos_json']
        cur.close(); return invoice_details
    finally:
        if conn: conn.close()

def get_all_invoices_with_details(user_id: str):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute('SELECT * FROM facturas WHERE user_id = %s ORDER BY fecha DESC, id DESC', (user_id,))
        facturas = cur.fetchall()
        invoices_list = []
        for f in facturas:
            details = dict(f); factura_id = details['id']
            cur.execute('SELECT descripcion, cantidad, precio_unitario FROM conceptos WHERE factura_id = %s', (factura_id,))
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
        query = "SELECT DISTINCT f.id, f.emisor, f.fecha, f.total FROM facturas f LEFT JOIN conceptos c ON f.id=c.factura_id WHERE f.user_id = %s"
        params = [user_id]
        if text_query:
            query += " AND (LOWER(f.emisor) LIKE %s OR LOWER(c.descripcion) LIKE %s)"
            params.extend([f'%{text_query.lower()}%', f'%{text_query.lower()}%'])
        if date_from:
            query += " AND TO_DATE(f.fecha, 'DD/MM/YYYY') >= TO_DATE(%s, 'YYYY-MM-DD')"
            params.append(date_from)
        if date_to:
            query += " AND TO_DATE(f.fecha, 'DD/MM/YYYY') <= TO_DATE(%s, 'YYYY-MM-DD')"
            params.append(date_to)
        
        query += " ORDER BY f.fecha DESC, f.id DESC"
        cur.execute(query, tuple(params))
        return [dict(row) for row in cur.fetchall()]
    finally:
        if conn: conn.close()

def delete_invoice(invoice_id: int, user_id: str):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM facturas WHERE id = %s AND user_id = %s RETURNING id", (invoice_id, user_id))
        was_deleted = cur.fetchone() is not None
        conn.commit()
        cur.close()
        return was_deleted
    except (Exception, psycopg2.DatabaseError) as error:
        print(f"Error borrando: {error}");
        if conn: conn.rollback()
        return False
    finally:
        if conn: conn.close()