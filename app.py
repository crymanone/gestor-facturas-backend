import os
import json
import io
import time
from flask import Flask, request, jsonify, g
from functools import wraps
from PIL import Image
import google.generativeai as genai
import database as db
from pypdf import PdfReader
import firebase_admin
from firebase_admin import credentials, auth

app = Flask(__name__)

try:
    firebase_sdk_json_str = os.environ.get("FIREBASE_ADMIN_SDK_JSON")
    if not firebase_sdk_json_str: raise ValueError("FIREBASE_ADMIN_SDK_JSON no configurada.")
    cred = credentials.Certificate(json.loads(firebase_sdk_json_str))
    if not firebase_admin._apps: firebase_admin.initialize_app(cred)
except Exception as e:
    print(f"ERROR CRÍTICO Firebase: {e}")

try:
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key: raise ValueError("No se encontró GOOGLE_API_KEY.")
    genai.configure(api_key=api_key)
except Exception as e:
    print(f"Error CRÍTICO Gemini: {e}")

gemini_model = genai.GenerativeModel('gemini-3.1-flash-lite')

def check_token(f):
    @wraps(f)
    def wrap(*args,**kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'ok': False, 'error': 'Token Bearer no encontrado'}), 401
        try:
            decoded_token = auth.verify_id_token(auth_header.split('Bearer ')[1])
            g.user_id = decoded_token['uid']
            g.user = db.get_or_create_user(decoded_token['uid'], decoded_token.get('email'))
        except Exception as e:
            return jsonify({'ok': False, 'error': f'Error auth: {e}'}), 403
        return f(*args, **kwargs)
    return wrap

def feature_protected(f):
    @wraps(f)
    def wrap(*args,**kwargs):
        status = db.get_user_status(g.user_id)
        if not status.get('is_active'):
            return jsonify({'ok': False, 'error': 'Acceso denegado. Período de prueba terminado.', 'user_status': status.get('status')}), 403
        return f(*args, **kwargs)
    return wrap

@app.route('/api/user/status', methods=['GET'])
@check_token
def user_status():
    try:
        status = db.get_user_status(g.user_id)
        return jsonify({"ok": True, "status": status})
    except Exception as e: return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/user/simulate_purchase', methods=['POST'])
@check_token
def simulate_purchase():
    conn = None
    try:
        conn = db.get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE users SET subscription_status = 'trial', trial_end_date = NOW() + INTERVAL '30 days' WHERE firebase_uid = %s", (g.user_id,))
        conn.commit(); cur.close()
        return jsonify({"ok": True, "message": "¡Pase Pionero activado! Tienes 30 días extra gratis."})
    except Exception as e: return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if conn: conn.close()

prompt_plantilla_factura = """
Actúa como un experto contable internacional. Analiza la factura y extrae los datos en formato JSON estricto.
INSTRUCCIONES CLAVE:
1. EXTRACCIÓN: Extrae `emisor`, `cif`, `fecha`, `total`, `base_imponible`.
2. MONEDA: Identifica el símbolo de la divisa utilizada (ej: €, $, £, MXN, COP, etc.) y guárdalo en el campo `"moneda"`. Si no lo encuentras, usa "€".
3. ESTADO (OBLIGATORIO): Examina evidencias de pago (PAGADO, PAID, recibo bancario). Si está pagada, pon `"estado": "Pagada"`. Si hay dudas o está pendiente, pon `"estado": "Pendiente"`.
4. CONCEPTOS (OBLIGATORIO): Extrae CADA concepto con `descripcion`, `cantidad` y `precio_unitario`. NUNCA dejes la lista vacía. Crea un concepto general si no hay detalle.

FORMATO JSON DE SALIDA ESTRICTO:
{ "emisor": "Nombre", "cif": "B123", "fecha": "DD/MM/AAAA", "total": 121.00, "base_imponible": 100.00, "estado": "Pagada", "moneda": "€", "conceptos":[ {"descripcion": "Producto", "cantidad": 2.0, "precio_unitario": 50.0} ] }
DEVUELVE ÚNICAMENTE EL CÓDIGO JSON, SIN TEXTO ADICIONAL ANTES NI DESPUÉS.
"""
prompt_multipagina_pdf = prompt_plantilla_factura 

@app.route('/api/process_invoice', methods=['POST'])
@check_token
@feature_protected
def process_document():
    if not request.data: return jsonify({"ok": False}), 400
    try:
        job_id = db.create_image_job(request.data, g.user_id)
        if job_id: return jsonify({"ok": True, "job_id": job_id})
        else: return jsonify({"ok": False}), 500
    except Exception as e: return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/upload_pdf', methods=['POST'])
@check_token
@feature_protected
def upload_pdf():
    if not request.data: return jsonify({"ok": False}), 400
    try:
        job_id = db.create_pdf_job(request.data, g.user_id)
        if job_id: return jsonify({"ok": True, "job_id": job_id})
        else: return jsonify({"ok": False}), 500
    except Exception as e: return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/job_status/<job_id>', methods=['GET'])
@check_token
def job_status(job_id):
    try:
        status = db.get_job_status(job_id, g.user_id)
        if status: return jsonify({"ok": True, "status": status})
        else: return jsonify({"ok": False}), 404
    except Exception as e: return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/process_queue', methods=['GET'])
def process_queue():
    auth_header = request.headers.get('Authorization')
    cron_secret = os.environ.get('CRON_SECRET')
    if not cron_secret or auth_header != f"Bearer {cron_secret}": return "Unauthorized", 401
    
    job = db.get_pending_job()
    if not job: return "Ok", 200
    
    job_id, job_data, user_id, job_type = job['id'], job['file_data'], job['user_id'], job['type']
    
    try:
        content_parts = []
        if job_type == 'pdf':
            pdf_stream = io.BytesIO(bytes(job_data))
            pdf_reader = PdfReader(pdf_stream)
            if not pdf_reader.pages: raise ValueError("PDF vacío")
            content_parts.append(prompt_multipagina_pdf)
            for page in pdf_reader.pages:
                if text := page.extract_text(): content_parts.append(text)
                for image_obj in page.images:
                    try: content_parts.append(Image.open(io.BytesIO(image_obj.data)))
                    except: pass
        elif job_type == 'image':
            img = Image.open(io.BytesIO(bytes(job_data)))
            content_parts = [prompt_plantilla_factura, img]
            
        response = gemini_model.generate_content(content_parts)
        
        raw_text = response.text
        start_idx = raw_text.find('{')
        end_idx = raw_text.rfind('}')
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_text = raw_text[start_idx:end_idx + 1]
            try: final_data = json.loads(json_text)
            except json.JSONDecodeError as e: raise ValueError(f"JSON inválido: {e}")
        else:
            raise ValueError("Sin JSON reconocible.")
        
        # --- EL CAMBIO: Ya no usamos Cloudinary, indicamos que es Local ---
        file_info = {"local_job_id": str(job_id), "format": job_type}
        
        invoice_id = db.add_invoice(final_data, f"gemini ({job_type})", user_id, file_info)
        if not invoice_id: raise ValueError("Error en BD")
        
        db.update_job_as_completed(job_id, final_data, job_type)
        return "Ok", 200
        
    except Exception as e:
        db.update_job_as_failed(job_id, str(e), job_type)
        return str(e), 500

@app.route('/api/invoices', methods=['GET', 'POST'])
@check_token
@feature_protected
def handle_invoices():
    if request.method == 'GET':
        docs = db.get_all_invoices(g.user_id)
        return jsonify({"ok": True, "invoices": docs})
    if request.method == 'POST':
        data = request.get_json()
        doc_data = {"emisor": data.get('emisor'), "cif": data.get('cif'), "fecha": data.get('fecha'), "total": data.get('total')}
        new_id = db.add_invoice(doc_data, "Manual", g.user_id)
        return jsonify({"ok": True, "id": new_id}) if new_id else (jsonify({"ok": False}), 500)

@app.route('/api/invoice/<int:doc_id>', methods=['GET', 'DELETE'])
@check_token
def handle_single_invoice(doc_id):
    if request.method == 'GET':
        details = db.get_invoice_details(doc_id, g.user_id)
        if details: return jsonify({"ok": True, "invoice": details})
        else: return jsonify({"ok": False}), 404
    if request.method == 'DELETE':
        success = db.delete_invoice(doc_id, g.user_id)
        if success: return jsonify({"ok": True})
        else: return jsonify({"ok": False}), 404

# --- EL COMPROBADOR DE ARCHIVOS LOCALES ---
@app.route('/api/invoice/<int:doc_id>/original', methods=['GET'])
@check_token
@feature_protected
def get_original_document(doc_id):
    try:
        details = db.get_invoice_details(doc_id, g.user_id)
        if not details or not details.get('file_info'): return jsonify({"ok": False}), 404
        return jsonify({"ok": True, "file_info": details['file_info']})
    except Exception as e: return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/invoice/<int:doc_id>/notes', methods=['PUT'])
@check_token
@feature_protected
def update_notes(doc_id):
    try:
        data = request.get_json()
        success = db.update_invoice_notes(doc_id, g.user_id, data.get('notas', ''))
        return jsonify({"ok": success})
    except: return jsonify({"ok": False}), 500

@app.route('/api/ai/query', methods=['POST'])
@check_token
@feature_protected
def ai_query():
    try:
        query_data = request.get_json()
        user_query = query_data.get('query', '')
        all_docs = db.get_all_invoices_with_details(g.user_id)
        if not all_docs: return jsonify({"ok": True, "answer": "No tienes facturas."})
        docs_context = json.dumps(all_docs, indent=2, ensure_ascii=False, default=str)
        prompt_contextual = f"""Actúa como un asistente financiero personal. DATOS: ```json\n{docs_context}\n```\nPREGUNTA: "{user_query}"
        INSTRUCCIONES: Responde en formato JSON: {{ "answer": "Tu respuesta", "invoice_id": 123 }}. Si el usuario pide VER una factura, pon su 'id'. Si no, pon null. Responde en el idioma del usuario."""
        response = gemini_model.generate_content(prompt_contextual)
        
        raw_text = response.text
        start_idx = raw_text.find('{'); end_idx = raw_text.rfind('}')
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            ai_data = json.loads(raw_text[start_idx:end_idx + 1])
            return jsonify({"ok": True, "answer": ai_data.get("answer"), "invoice_id": ai_data.get("invoice_id")})
        else: return jsonify({"ok": True, "answer": raw_text})
    except Exception as e: return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/search', methods=['POST'])
@check_token
def search():
    try:
        data = request.get_json()
        results = db.search_invoices(g.user_id, data.get('text_query'), data.get('date_from'), data.get('date_to'))
        return jsonify({"ok": True, "invoices": results})
    except: return jsonify({"ok": False}), 500

if __name__ == '__main__':
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))