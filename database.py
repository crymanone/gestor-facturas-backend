# database.py - VERSIÓN CORREGIDA CON MANEJO DE TEXTO PARA user_id
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
        
        # Tabla de facturas
        cur.execute('''
            CREATE TABLE IF NOT EXISTS facturas (
                id BIGSERIAL PRIMARY KEY, 
                emisor TEXT, 
                cif TEXT, 
                fecha TEXT, 
                total REAL, 
                base_imponible REAL, 
                impuestos_json JSONB, 
                ia_model TEXT, 
                user_id TEXT, 
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        ''')
        
        # Índices
        cur.execute('CREATE INDEX IF NOT EXISTS idx_facturas_user_id ON facturas(user_id);')
        
        # Tabla de conceptos
        cur.execute('''
            CREATE TABLE IF NOT EXISTS conceptos (
                id BIGSERIAL PRIMARY KEY, 
                factura_id BIGINT REFERENCES facturas(id) ON DELETE CASCADE, 
                descripcion TEXT, 
                cantidad REAL, 
                precio_unitario REAL,
                user_id TEXT
            )
        ''')
        
        # Tabla de procesamiento de PDFs
        cur.execute('''
            CREATE TABLE IF NOT EXISTS pdf_processing_queue (
                id UUID PRIMARY KEY,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                status TEXT NOT NULL,
                pdf_data BYTEA,
                result_json JSONB,
                error_message TEXT,
                user_id TEXT,
                type TEXT DEFAULT 'pdf'
            )
        ''')
        
        # Tabla de procesamiento de imágenes
        cur.execute('''
            CREATE TABLE IF NOT EXISTS image_processing_queue (
                id UUID PRIMARY KEY,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                status TEXT NOT NULL,
                image_data BYTEA,
                result_json JSONB,
                error_message TEXT,
                user_id TEXT,
                type TEXT DEFAULT 'image'
            )
        ''')
        
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
    sql_concepto = "INSERT INTO conceptos (factura_id, descripcion, cantidad, precio_unitario, user_id) VALUES (%s, %s, %s, %s, %s);"
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
                # FILTRAR: No insertar conceptos con descripción vacía o nula
                descripcion = concepto.get('descripcion', '').strip()
                if descripcion:  # Solo insertar si hay descripción
                    cur.execute(sql_concepto, (
                        factura_id, 
                        descripcion,
                        to_float(concepto.get('cantidad')), 
                        to_float(concepto.get('precio_unitario')),
                        user_id
                    ))
                    print(f"💾 Concepto guardado: {descripcion}")
                else:
                    print(f"⚠️ Concepto omitido (descripción vacía): {concepto}")
        
        conn.commit(); cur.close(); return factura_id
    except (Exception, psycopg2.DatabaseError) as error:
        print(f"Error DB en add_invoice: {error}");
        if conn: conn.rollback()
        return None
    finally:
        if conn: conn.close()

def create_pdf_job(pdf_data, user_id: str):
    job_id = str(uuid.uuid4())
    sql = "INSERT INTO pdf_processing_queue (id, status, pdf_data, user_id, type) VALUES (%s, 'pending', %s, %s, 'pdf');"
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

def create_image_job(image_data, user_id: str):
    job_id = str(uuid.uuid4())
    sql = "INSERT INTO image_processing_queue (id, status, image_data, user_id, type) VALUES (%s, 'pending', %s, %s, 'image');"
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(sql, (job_id, psycopg2.Binary(image_data), user_id))
        conn.commit()
        cur.close()
        return job_id
    finally:
        if conn: conn.close()

def get_job_status(job_id, user_id):
    # Buscar en ambas tablas
    sql_pdf = "SELECT status, result_json, error_message, 'pdf' as type FROM pdf_processing_queue WHERE id = %s AND user_id = %s;"
    sql_image = "SELECT status, result_json, error_message, 'image' as type FROM image_processing_queue WHERE id = %s AND user_id = %s;"
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # CORRECCIÓN: Usar el job_id como texto directamente
        cur.execute(sql_pdf, (job_id, user_id))
        job = cur.fetchone()
        
        # Si no está en PDFs, buscar en imágenes
        if not job:
            cur.execute(sql_image, (job_id, user_id))
            job = cur.fetchone()
        
        cur.close()
        return dict(job) if job else None
    except Exception as e:
        print(f"❌ ERROR en get_job_status: {e}")
        return None
    finally:
        if conn: conn.close()
        
def get_pending_job():
    # Obtener trabajo pendiente de ambas tablas
    sql = """
    (SELECT id, pdf_data as file_data, user_id, 'pdf' as type 
     FROM pdf_processing_queue 
     WHERE status = 'pending' 
     ORDER BY created_at 
     LIMIT 1)
    UNION ALL
    (SELECT id, image_data as file_data, user_id, 'image' as type 
     FROM image_processing_queue 
     WHERE status = 'pending' 
     ORDER BY created_at 
     LIMIT 1)
    LIMIT 1;
    """
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(sql)
        job = cur.fetchone()
        cur.close()
        return dict(job) if job else None
    finally:
        if conn: conn.close()

def update_job_as_completed(job_id, result_json, job_type):
    table_name = "pdf_processing_queue" if job_type == "pdf" else "image_processing_queue"
    # CORRECCIÓN: Limpiar los datos binarios y actualizar el estado
    sql = f"UPDATE {table_name} SET status = 'completed', result_json = %s, pdf_data = NULL, image_data = NULL WHERE id = %s;"
    conn = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute(sql, (json.dumps(result_json), job_id))
        conn.commit(); cur.close()
    finally:
        if conn: conn.close()

def update_job_as_failed(job_id, error_message, job_type):
    table_name = "pdf_processing_queue" if job_type == "pdf" else "image_processing_queue"
    # CORRECCIÓN: Limpiar los datos binarios y actualizar el estado
    sql = f"UPDATE {table_name} SET status = 'failed', error_message = %s, pdf_data = NULL, image_data = NULL WHERE id = %s;"
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
        
        # Obtener factura - CORREGIR: usar user_id en ambas tablas
        cur.execute('''
            SELECT f.* 
            FROM facturas f 
            WHERE f.id = %s AND f.user_id = %s
        ''', (invoice_id, user_id))
        invoice = cur.fetchone()
        if not invoice: 
            return None
        
        # Obtener conceptos - CORREGIR: usar user_id también aquí
        cur.execute('''
            SELECT descripcion, cantidad, precio_unitario 
            FROM conceptos 
            WHERE factura_id = %s AND user_id = %s
            AND descripcion IS NOT NULL  -- ← Filtrar conceptos nulos
        ''', (invoice_id, user_id))
        
        conceptos = []
        for row in cur.fetchall():
            concepto = dict(row)
            # Filtrar conceptos con descripción vacía o nula
            if concepto.get('descripcion') and concepto['descripcion'].strip():
                conceptos.append(concepto)
        
        print(f"🔍 Conceptos encontrados para factura {invoice_id}: {conceptos}")
        
        # Construir respuesta
        invoice_details = dict(invoice)
        invoice_details['conceptos'] = conceptos
        
        # Manejar impuestos
        impuestos_json = invoice_details.get('impuestos_json')
        if impuestos_json and isinstance(impuestos_json, str):
            try:
                invoice_details['impuestos'] = json.loads(impuestos_json)
            except json.JSONDecodeError:
                invoice_details['impuestos'] = {}
        else:
            invoice_details['impuestos'] = impuestos_json or {}
        
        # Limpiar campo temporal
        if 'impuestos_json' in invoice_details:
            del invoice_details['impuestos_json']
        
        cur.close()
        return invoice_details
    except Exception as e:
        print(f"Error en get_invoice_details: {e}")
        return None
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
            cur.execute('SELECT descripcion, cantidad, precio_unitario FROM conceptos WHERE factura_id = %s AND user_id = %s', (factura_id, user_id))
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