# app.py - VERSIÓN COMPLETA, FUNCIONAL Y RESTAURADA (SIN MAPEO DE USUARIO)
import os
import json
import io
from flask import Flask, request, jsonify, g
from functools import wraps
from PIL import Image
import google.generativeai as genai
import database as db
from pypdf import PdfReader
import firebase_admin
from firebase_admin import credentials, auth

app = Flask(__name__)

# --- INICIALIZACIÓN DE SERVICIOS EXTERNOS ---
try:
    firebase_sdk_json_str = os.environ.get("FIREBASE_ADMIN_SDK_JSON")
    if not firebase_sdk_json_str:
        raise ValueError("La variable de entorno FIREBASE_ADMIN_SDK_JSON no está configurada.")
    cred_json = json.loads(firebase_sdk_json_str)
    cred = credentials.Certificate(cred_json)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    print("Firebase Admin SDK inicializado correctamente.")
except Exception as e:
    print(f"ERROR CRÍTICO al inicializar Firebase: {e}")

try:
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("No se encontró GOOGLE_API_KEY en las variables de entorno.")
    genai.configure(api_key=api_key)
    print("Google Gemini API configurada correctamente.")
except Exception as e:
    print(f"Error CRÍTICO al configurar Gemini: {e}")

# --- FUNCIÓN DE AUTENTICACIÓN (USA FIREBASE UID DIRECTAMENTE) ---
def check_token(f):
    @wraps(f)
    def wrap(*args,**kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'ok': False, 'error': 'No se proveyó un token Bearer'}), 401
        try:
            token = auth_header.split('Bearer ')[1]
            decoded_token = auth.verify_id_token(token)
            # Se asigna directamente el uid de Firebase a g.user_id
            g.user_id = decoded_token['uid']
        except auth.ExpiredIdTokenError:
            return jsonify({'ok': False, 'error': 'El token ha expirado'}), 403
        except auth.InvalidIdTokenError as e:
            return jsonify({'ok': False, 'error': f'Token inválido: {e}'}), 403
        except Exception as e:
            return jsonify({'ok': False, 'error': f'Error de autenticación: {e}'}), 403
        return f(*args, **kwargs)
    return wrap

with app.app_context():
    db.init_db()

gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')

prompt_plantilla_factura = """...""" # Tu prompt aquí
prompt_multipagina_pdf = """...""" # Tu prompt aquí

# --- RUTAS DE LA API ---

@app.route('/api/process_invoice', methods=['POST'])
@check_token
def process_invoice():
    if not request.data:
        return jsonify({"ok": False, "error": "No se ha enviado ninguna imagen"}), 400
    try:
        job_id = db.create_image_job(request.data, g.user_id)
        return jsonify({"ok": True, "job_id": job_id}) if job_id else jsonify({"ok": False, "error": "No se pudo crear el trabajo."}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500

@app.route('/api/upload_pdf', methods=['POST'])
@check_token
def upload_pdf():
    if not request.data:
        return jsonify({"ok": False, "error": "No se ha enviado ningún fichero PDF"}), 400
    try:
        job_id = db.create_pdf_job(request.data, g.user_id)
        return jsonify({"ok": True, "job_id": job_id}) if job_id else jsonify({"ok": False, "error": "No se pudo crear el trabajo."}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500

@app.route('/api/job_status/<job_id>', methods=['GET'])
@check_token
def job_status(job_id):
    try:
        status = db.get_job_status(job_id, g.user_id)
        return jsonify({"ok": True, "status": status}) if status else jsonify({"ok": False, "error": "Job ID no encontrado."}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500

@app.route('/api/process_queue', methods=['GET'])
def process_queue():
    auth_header = request.headers.get('Authorization')
    cron_secret = os.environ.get('CRON_SECRET')
    if not cron_secret or auth_header != f"Bearer {cron_secret}":
        return "Unauthorized", 401
    job = db.get_pending_job()
    if not job: return "No hay trabajos pendientes.", 200
    job_id, job_data, user_id, job_type = job['id'], job['file_data'], job['user_id'], job['type']
    json_text = ""
    try:
        content_parts = []
        if job_type == 'pdf':
            pdf_stream = io.BytesIO(bytes(job_data)); pdf_reader = PdfReader(pdf_stream)
            if not pdf_reader.pages: raise ValueError("PDF vacío.")
            content_parts.append(prompt_multipagina_pdf)
            for page in pdf_reader.pages:
                if text := page.extract_text(): content_parts.append(text)
                for image_obj in page.images:
                    try: content_parts.append(Image.open(io.BytesIO(image_obj.data)))
                    except Exception as e: print(f"⚠️ No se pudo procesar imagen en PDF: {e}")
        elif job_type == 'image':
            img = Image.open(io.BytesIO(bytes(job_data)))
            content_parts = [prompt_plantilla_factura, img]
        if len(content_parts) <= 1: raise ValueError("No se extrajo contenido del documento.")
        response = gemini_model.generate_content(content_parts)
        json_text = response.text.replace('```json', '').replace('```', '').strip()
        final_invoice_data = json.loads(json_text)
        invoice_id = db.add_invoice(final_invoice_data, f"gemini-1.5-flash ({job_type})", user_id)
        if not invoice_id: raise ValueError("Falló el guardado en BD.")
        db.update_job_as_completed(job_id, final_invoice_data, job_type)
        return f"Job {job_id} procesado.", 200
    except Exception as e:
        db.update_job_as_failed(job_id, f"Error: {e}. JSON recibido: {json_text}", job_type)
        return f"Error en job {job_id}: {e}", 500

@app.route('/api/invoices', methods=['GET', 'POST'])
@check_token
def handle_invoices():
    if request.method == 'GET':
        invoices = db.get_all_invoices(g.user_id)
        return jsonify({"ok": True, "invoices": invoices})
    if request.method == 'POST':
        invoice_data = request.get_json()
        if not invoice_data or not 'emisor' in invoice_data: return jsonify({"ok": False, 'error': "Datos inválidos"}), 400
        new_id = db.add_invoice(invoice_data, "Manual", g.user_id)
        return jsonify({"ok": True, "id": new_id}) if new_id else jsonify({"ok": False, "error": "No se pudo guardar"}), 500

@app.route('/api/invoice/<int:invoice_id>', methods=['GET', 'DELETE'])
@check_token
def handle_single_invoice(invoice_id):
    if request.method == 'GET':
        details = db.get_invoice_details(invoice_id, g.user_id)
        return jsonify({"ok": True, "invoice": details}) if details else jsonify({"ok": False, "error": "No encontrada"}), 404
    if request.method == 'DELETE':
        success = db.delete_invoice(invoice_id, g.user_id)
        return jsonify({"ok": True}) if success else jsonify({"ok": False, "error": "No se pudo borrar"}), 404

@app.route('/api/invoice/<int:invoice_id>/notes', methods=['PUT'])
@check_token
def update_invoice_notes(invoice_id):
    data = request.get_json()
    if 'notas' not in data: return jsonify({"ok": False, "error": "Falta el campo 'notas'"}), 400
    success = db.update_invoice_notes(invoice_id, g.user_id, data['notas'])
    return jsonify({"ok": True}) if success else jsonify({"ok": False, "error": "No encontrada"}), 404

@app.route('/api/search', methods=['POST'])
@check_token
def search():
    data = request.get_json()
    text_query = data.get('text_query'); date_from_raw = data.get('date_from'); date_to_raw = data.get('date_to')
    date_from = f"{date_from_raw[6:10]}-{date_from_raw[3:5]}-{date_from_raw[0:2]}" if date_from_raw else None
    date_to = f"{date_to_raw[6:10]}-{date_to_raw[3:5]}-{date_to_raw[0:2]}" if date_to_raw else None
    results = db.search_invoices(g.user_id, text_query, date_from, date_to)
    return jsonify({"ok": True, "invoices": results})

@app.route('/api/ask', methods=['POST'])
@check_token
def ask_assistant():
    data = request.get_json()
    if not data or 'query' not in data: return jsonify({"ok": False, "error": "No hay pregunta"}), 400
    invoices = db.get_all_invoices_with_details(g.user_id)
    if not invoices: return jsonify({"ok": True, "answer": "No tienes facturas."})
    context = json.dumps(invoices, default=str)
    prompt = f"Contexto: {context}\n\nPregunta: {data['query']}\n\nRespuesta:"
    response = gemini_model.generate_content(prompt)
    return jsonify({"ok": True, "answer": response.text})

if __name__ == '__main__':
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))