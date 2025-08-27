# app.py - VERSIÓN CORREGIDA CON MANEJO DE IMÁGENES Y PDFS
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

# --- INICIALIZACIÓN Y CONFIGURACIÓN ---
app = Flask(__name__)
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

# Inicializa la base de datos al arrancar la app
with app.app_context():
    db.init_db()

gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')

# --- DECORADOR DE AUTENTICACIÓN ---
def check_token(f):
    @wraps(f)
    def wrap(*args,**kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'ok': False, 'error': 'No se proveyó un token Bearer'}), 401
        try:
            token = auth_header.split('Bearer ')[1]
            decoded_token = auth.verify_id_token(token)
            g.user_id = decoded_token['uid']  # Este es el UID de Firebase (texto)
        except auth.ExpiredIdTokenError:
            return jsonify({'ok': False, 'error': 'El token ha expirado'}), 403
        except auth.InvalidIdTokenError as e:
            return jsonify({'ok': False, 'error': f'Token inválido: {e}'}), 403
        except Exception as e:
            return jsonify({'ok': False, 'error': f'Error de autenticación: {e}'}), 403
        return f(*args, **kwargs)
    return wrap

# --- PROMPTS PARA LA IA ---
prompt_plantilla_factura = """
Actúa como un experto contable especializado en la extracción de datos de documentos.
Analiza la siguiente imagen de una factura o ticket.
Extrae los siguientes campos y devuelve la respuesta estrictamente en formato JSON, sin texto introductorio, explicaciones o marcado de código.
Los campos a extraer son:
- emisor (El nombre de la empresa o persona que emite la factura)
- cif (El identificador fiscal: CIF, NIF, VAT ID, etc.)
- fecha (La fecha de emisión del documento en formato DD/MM/AAAA)
- total (El importe total final pagado, como un número flotante)
- base_imponible (El subtotal antes de impuestos, como un número flotante)
- impuestos (Un objeto JSON con los diferentes tipos de impuesto y su valor. Ej: {"iva_21": 21.00, "otros_impuestos": 2.50})
- conceptos (Una lista de objetos, donde cada objeto contiene 'descripcion', 'cantidad' y 'precio_unitario')
Si un campo no se puede encontrar o no es aplicable, devuélvelo como `null`.
Si los conceptos son difíciles de desglosar, extrae al menos una descripción general como un único concepto.
"""

prompt_multipagina_pdf = """
Actúa como un experto contable. Te proporciono una serie de textos e imágenes extraídos de las páginas de UNA ÚNICA factura en PDF.
Analiza todo el contenido en conjunto para obtener una respuesta final y unificada.
Extrae los siguientes campos y devuelve la respuesta estrictamente en formato JSON:
- emisor, cif, fecha, total, base_imponible, impuestos, conceptos.
Si un campo aparece en varias páginas (ej. 'emisor'), usa el de la primera aparición. Si los conceptos se reparten en varias páginas, combínalos todos en una sola lista. El 'total' y la 'base_imponible' suelen estar en la última página; prioriza esos.
Si un campo no se puede encontrar en ninguna página, devuélvelo como `null`.
"""

# --- RUTAS DE LA API ---

@app.route('/api/process_invoice', methods=['POST'])
@check_token
def process_invoice():
    if not request.data:
        return jsonify({"ok": False, "error": "No se ha enviado ninguna imagen"}), 400
    
    try:
        job_id = db.create_image_job(request.data, g.user_id)
        if job_id:
            return jsonify({"ok": True, "job_id": job_id})
        else:
            return jsonify({"ok": False, "error": "No se pudo crear el trabajo."}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500

@app.route('/api/upload_pdf', methods=['POST'])
@check_token
def upload_pdf():
    if not request.data:
        return jsonify({"ok": False, "error": "No se ha enviado ningún fichero PDF"}), 400
    
    try:
        job_id = db.create_pdf_job(request.data, g.user_id)
        if job_id:
            return jsonify({"ok": True, "job_id": job_id})
        else:
            return jsonify({"ok": False, "error": "No se pudo crear el trabajo."}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500

@app.route('/api/job_status/<job_id>', methods=['GET'])
@check_token
def job_status(job_id):
    try:
        status = db.get_job_status(job_id, g.user_id)
        if status:
            return jsonify({"ok": True, "status": status})
        else:
            return jsonify({"ok": False, "error": "Job ID no encontrado o no te pertenece."}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500

@app.route('/api/process_queue', methods=['GET'])
def process_queue():
    auth_header = request.headers.get('Authorization')
    cron_secret = os.environ.get('CRON_SECRET')
    if not cron_secret or auth_header != f"Bearer {cron_secret}":
        return "Unauthorized", 401

    job = db.get_pending_job()
    if not job:
        return "No hay trabajos pendientes.", 200
    
    job_id, job_data, user_id, job_type = job['id'], job['data'], job['user_id'], job['type']

    try:
        content_parts = []
        if job_type == 'pdf':
            pdf_stream = io.BytesIO(bytes(job_data))
            pdf_reader = PdfReader(pdf_stream)
            if not pdf_reader.pages:
                raise ValueError("PDF vacío.")
            content_parts.append(prompt_multipagina_pdf)
            for page in pdf_reader.pages:
                if text := page.extract_text():
                    content_parts.append(text)
                for image_obj in page.images:
                    content_parts.append(Image.open(io.BytesIO(image_obj.data)))
        elif job_type == 'image':
            image_bytes = io.BytesIO(bytes(job_data))
            img = Image.open(image_bytes)
            content_parts = [prompt_plantilla_factura, img]

        if len(content_parts) <= 1:
            raise ValueError("No se extrajo contenido.")
        
        response = gemini_model.generate_content(content_parts)
        json_text = response.text.replace('```json', '').replace('```', '').strip()
        final_invoice_data = json.loads(json_text)
        
        invoice_id = db.add_invoice(final_invoice_data, f"gemini-1.5-flash ({job_type})", user_id)
        if not invoice_id:
            raise ValueError("Falló el guardado en BBDD.")
        
        db.update_job_as_completed(job_id, final_invoice_data, job_type)
        return f"Job {job_id} procesado.", 200
    except Exception as e:
        db.update_job_as_failed(job_id, str(e), job_type)
        return f"Fallo en job {job_id}: {str(e)}", 500

@app.route('/api/invoices', methods=['GET', 'POST'])
@check_token
def handle_invoices():
    if request.method == 'GET':
        try:
            invoices = db.get_all_invoices(g.user_id)
            return jsonify({"ok": True, "invoices": invoices})
        except Exception as e:
            return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500
            
    if request.method == 'POST':
        try:
            invoice_data = request.get_json()
            if not invoice_data or not invoice_data.get('emisor'):
                return jsonify({"ok": False, 'error': "Datos inválidos o emisor faltante"}), 400
            new_id = db.add_invoice(invoice_data, "Manual", g.user_id)
            if new_id:
                return jsonify({"ok": True, "id": new_id}), 201
            else:
                return jsonify({"ok": False, "error": "No se pudo guardar la factura"}), 500
        except Exception as e:
            return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500

@app.route('/api/invoice/<int:invoice_id>', methods=['GET', 'DELETE'])
@check_token
def handle_single_invoice(invoice_id):
    if request.method == 'GET':
        try:
            details = db.get_invoice_details(invoice_id, g.user_id)
            if details:
                return jsonify({"ok": True, "invoice": details})
            else:
                return jsonify({"ok": False, "error": "Factura no encontrada"}), 404
        except Exception as e:
            return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500
            
    if request.method == 'DELETE':
        try:
            success = db.delete_invoice(invoice_id, g.user_id)
            if success:
                return jsonify({"ok": True, "message": "Factura borrada"})
            else:
                return jsonify({"ok": False, "error": "No se pudo borrar"}), 404
        except Exception as e:
            return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500

@app.route('/api/search', methods=['POST'])
@check_token
def search():
    try:
        data = request.get_json()
        text_query = data.get('text_query')
        date_from_raw = data.get('date_from')
        date_to_raw = data.get('date_to')
        
        date_from = f"{date_from_raw[6:10]}-{date_from_raw[3:5]}-{date_from_raw[0:2]}" if date_from_raw else None
        date_to = f"{date_to_raw[6:10]}-{date_to_raw[3:5]}-{date_to_raw[0:2]}" if date_to_raw else None
        
        results = db.search_invoices(g.user_id, text_query, date_from, date_to)
        return jsonify({"ok": True, "invoices": results})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500

@app.route('/api/ask', methods=['POST'])
@check_token
def ask_assistant():
    try:
        query_data = request.get_json()
        if not query_data or 'query' not in query_data:
            return jsonify({"ok": False, "error": "No se ha proporcionado ninguna pregunta."}), 400
            
        user_query = query_data['query']
        all_invoices = db.get_all_invoices_with_details(g.user_id)
        
        if not all_invoices:
            return jsonify({"ok": True, "answer": "No tienes ninguna factura registrada todavía."})
            
        invoices_context = json.dumps(all_invoices, indent=2, ensure_ascii=False, default=str)
        prompt_contextual = f"""
        Actúa como un asistente experto en contabilidad y finanzas personales.
        A continuación, te proporciono una lista de las facturas de un usuario en formato JSON.
        Tu tarea es responder a la pregunta del usuario basándote únicamente en estos datos.
        DATOS DE LAS FACTURAS:
        ```json
        {invoices_context}
        ```
        PREGUNTA DEL USUARIO:
        "{user_query}"
        Proporciona una respuesta clara, concisa y directa. Si la pregunta no se puede responder con los datos proporcionados, indica amablemente que no tienes esa información. No inventes datos.
        """
        
        response = gemini_model.generate_content(prompt_contextual)
        return jsonify({"ok": True, "answer": response.text})
        
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))