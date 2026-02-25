import os
import psycopg2
import psycopg2.extras
import json
import uuid
from datetime import datetime, timedelta

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
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                firebase_uid TEXT NOT NULL UNIQUE,
                email TEXT,
                trial_start_date TIMESTAMPTZ,
                trial_end_date TIMESTAMPTZ,
                subscription_status TEXT DEFAULT 'trial',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        ''')
        # Añadida la columna moneda
        cur.execute('''
            CREATE TABLE IF NOT EXISTS facturas (
                id BIGSERIAL PRIMARY KEY, emisor TEXT, cif TEXT, fecha TEXT, 
                total REAL, base_imponible REAL, impuestos_json JSONB, 
                ia_model TEXT, user_id TEXT, created_at TIMESTAMPTZ DEFAULT NOW(),
                estado TEXT DEFAULT 'Pendiente', notas TEXT,
                file_info JSONB, moneda TEXT DEFAULT '€'
            )
        ''')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_facturas_user_id ON facturas(user_id);')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS conceptos (
                id BIGSERIAL PRIMARY KEY, factura_id BIGINT REFERENCES facturas(id) ON DELETE CASCADE, 
                descripcion TEXT, cantidad REAL, precio_unitario REAL, user_id TEXT
            )
        ''')
        cur.execute('CREATE TABLE IF NOT EXISTS pdf_processing_queue (id UUID PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT NOW(), status TEXT NOT NULL, pdf_data BYTEA, result_json JSONB, error_message TEXT, user_id TEXT, type TEXT DEFAULT \'pdf\')')
        cur.execute('CREATE TABLE IF NOT EXISTS image_processing_queue (id UUID PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT NOW(), status TEXT NOT NULL, image_data BYTEA, result_json JSONB, error_message TEXT, user_id TEXT, type TEXT DEFAULT \'image\')')
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"Error al inicializar la base de datos: {e}")
    finally:
        if conn: conn.close()

def get_or_create_user(firebase_uid: str, email: str = None):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT * FROM users WHERE firebase_uid = %s", (firebase_uid,))
        user = cur.fetchone()
        if user:
            return dict(user)
        else:
            start = datetime.utcnow()
            end = start + timedelta(days=7)
            cur.execute(
                "INSERT INTO users (firebase_uid, email, trial_start_date, trial_end_date, subscription_status) VALUES (%s, %s, %s, %s, 'trial') RETURNING *",
                (firebase_uid, email, start, end)
            )
            new_user = cur.fetchone()
            conn.commit()
            return dict(new_user)
    finally:
        if conn: conn.close()

def get_user_status(firebase_uid: str):
    user = get_or_create_user(firebase_uid)
    if not user: return {'status': 'not_found'}
    status = user['subscription_status']
    is_active = False
    if status == 'trial':
        if datetime.utcnow().replace(tzinfo=None) < user['trial_end_date'].replace(tzinfo=None):
            is_active = True
            return {'status': 'trial_active', 'trial_end_date': user['trial_end_date'].isoformat(), 'is_active': is_active}
        else:
            conn = None
            try:
                conn = get_db_connection(); cur = conn.cursor()
                cur.execute("UPDATE users SET subscription_status = 'trial_expired' WHERE firebase_uid = %s", (firebase_uid,))
                conn.commit()
            finally:
                if conn: conn.close()
            return {'status': 'trial_expired', 'is_active': False}
    elif status in['active', 'subscribed']:
        return {'status': 'subscribed', 'is_active': True}
    else: return {'status': status, 'is_active': False}

def to_float(value):
    if value is None: return 0.0
    try: return float(value)
    except (ValueError, TypeError): return 0.0

def add_invoice(invoice_data: dict, ia_model: str, user_id: str, file_info: dict = None):
    sql_factura = """
    INSERT INTO facturas (emisor, cif, fecha, total, base_imponible, impuestos_json, ia_model, user_id, estado, file_info, moneda)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
    """
    sql_concepto = "INSERT INTO conceptos (factura_id, descripcion, cantidad, precio_unitario, user_id) VALUES (%s, %s, %s, %s, %s);"
    conn = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        impuestos_str = json.dumps(invoice_data.get('impuestos')) if isinstance(invoice_data.get('impuestos'), dict) else None
        estado = invoice_data.get('estado', 'Pendiente')
        moneda = invoice_data.get('moneda', '€') # Obtenemos la moneda, por defecto €
        file_info_str = json.dumps(file_info) if file_info else None

        cur.execute(sql_factura, (
            invoice_data.get('emisor'), invoice_data.get('cif'), invoice_data.get('fecha'),
            to_float(invoice_data.get('total')), to_float(invoice_data.get('base_imponible')),
            impuestos_str, ia_model, user_id, estado, file_info_str, moneda
        ))
        factura_id = cur.fetchone()[0]
        conceptos_list = invoice_data.get('conceptos',