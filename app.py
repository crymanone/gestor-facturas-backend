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

# (Inicialización de Firebase y Gemini sin cambios)
try:
    firebase_sdk_json_str = os.environ.get("FIREBASE_ADMIN_SDK_JSON")
    if not firebase_sdk_json_str: raise ValueError("FIREBASE_ADMIN_SDK_JSON no configurada.")
    cred = credentials.Certificate(json.loads(firebase_sdk_json_str))
    if not firebase_admin._apps: firebase_admin.initialize_app(cred)
    print("Firebase Admin SDK inicializado.")
except Exception as e:
    print(f"ERROR CRÍTICO al inicializar Firebase: {e}")
try:
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key: raise ValueError("No se encontró GOOGLE_API_KEY.")
    genai.configure(api_key=api_key)
    print("Google Gemini API configurada.")
except Exception as e:
    print(f"Error CRÍTICO al configurar Gemini: {e}")
# (init_db y modelo Gemini sin cambios)
with app.app_context():
    db.init_db()
gemini_model = genai.GenerativeModel('gemini-2.5-flash')

def check_token(f):
    @wraps(f)
    def wrap(*args,**kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'ok': False, 'error': 'Token Bearer no encontrado'}), 401
        try:
            decoded_token = auth.verify_id_token(auth_header.split('Bearer ')[1])
            g.user_id = decoded_token['uid']
        except Exception as e:
            return jsonify({'ok': False, 'error': f'Error de autenticación: {e}'}), 403
        return f(*args, **kwargs)
    return wrap

# --- MODIFICADO: Prompt de IA mejorado para detectar estado de pago ---
prompt_plantilla_factura = """
Eres un experto contable con capacidades de detective. Analiza la factura y extrae los datos en formato JSON.

INSTRUCCIONES CLAVE:
1.  **EXTRACCIÓN DE DATOS BÁSICOS**: Extrae `emisor`, `cif`, `fecha`, `total`, `base_imponible`, y `conceptos`.
2.  **ANÁLISIS DE ESTADO (OBLIGATORIO)**:
    -   Examina el documento en busca de CUALQUIER evidencia de pago. Busca palabras como "PAGADO", "ABONADO", "CANCELADO", "IMPORTE PENDIENTE: 0", sellos de "RECIBIDO" o "PAGADO", o firmas que lo indiquen.
    -   Si encuentras evidencia clara de pago, añade el campo `"estado": "Pagada"`.
    -   Si NO encuentras evidencia clara, o ves términos como "pendiente", "vencimiento", "a pagar", añade el campo `"estado": "Pendiente"`.
    -   Este campo es OBLIGATORIO.
3.  **CONCEPTOS (OBLIGATORIO)**:
    -   Extrae CADA concepto con `descripcion`, `cantidad` y `precio_unitario`.
    -   Si no hay conceptos detallados, crea UNO general (ej. "Servicio general") usando el `total` como `precio_unitario`. NUNCA dejes la lista de conceptos vacía.

FORMATO JSON DE SALIDA ESTRICTO:
{
  "emisor": "Nombre de la Empresa S.L.",
  "cif": "B12345678",
  "fecha": "DD/MM/AAAA",
  "total": 121.00,
  "base_imponible": 100.00,
  "estado": "Pagada",
  "conceptos": [
    {"descripcion": "Producto A", "cantidad": 2.0, "precio_unitario": 50.0}
  ]
}
"""
# (El resto de los endpoints hasta /api/invoice/<int:invoice_id> sin cambios significativos)
prompt_multipagina_pdf = prompt_plantilla_factura # Usamos el mismo prompt mejorado
@app.route('/api/process_invoice', methods=['POST'])
@check_token
def process_invoice():
    if not request.data: return jsonify({"ok": False, "error": "No se ha enviado ninguna imagen"}), 400
    try:
        job_id = db.create_image_job(request.data, g.user_id)
        if job_id: return jsonify({"ok": True, "job_id": job_id})
        else: return jsonify({"ok": False, "error": "No se pudo crear el trabajo."}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500
@app.route('/api/upload_pdf', methods=['POST'])
@check_token
def upload_pdf():
    if not request.data: return jsonify({"ok": False, "error": "No se ha enviado ningún fichero PDF"}), 400
    try:
        job_id = db.create_pdf_job(request.data, g.user_id)
        if job_id: return jsonify({"ok": True, "job_id": job_id})
        else: return jsonify({"ok": False, "error": "No se pudo crear el trabajo."}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500
@app.route('/api/job_status/<job_id>', methods=['GET'])
@check_token
def job_status(job_id):
    try:
        status = db.get_job_status(job_id, g.user_id)
        if status: return jsonify({"ok": True, "status": status})
        else: return jsonify({"ok": False, "error": "Job ID no encontrado o no te pertenece."}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500
@app.route('/api/process_queue', methods=['GET'])
def process_queue():
    auth_header = request.headers.get('Authorization')
    cron_secret = os.environ.get('CRON_SECRET')
    if not cron_secret or auth_header != f"Bearer {cron_secret}": return "Unauthorized", 401
    job = db.get_pending_job()
    if not job: return "No hay trabajos pendientes.", 200
    job_id, job_data, user_id, job_type = job['id'], job['file_data'], job['user_id'], job['type']
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
        invoice_id = db.add_invoice(final_invoice_data, f"gemini-2.0-flash ({job_type})", user_id)
        if not invoice_id: raise ValueError("Falló el guardado en la base de datos.")
        db.update_job_as_completed(job_id, final_invoice_data, job_type)
        return f"Job {job_id} procesado.", 200
    except Exception as e:
        db.update_job_as_failed(job_id, f"Error procesando documento: {str(e)}", job_type)
        return f"Error en job {job_id}: {str(e)}", 500
@app.route('/api/invoices', methods=['GET', 'POST'])
@check_token
def handle_invoices():
    if request.method == 'GET':
        invoices = db.get_all_invoices(g.user_id)
        return jsonify({"ok": True, "invoices": invoices})
    if request.method == 'POST':
        invoice_data = request.get_json()
        if not invoice_data or not invoice_data.get('emisor'):
            return jsonify({"ok": False, 'error': "Datos inválidos"}), 400
        new_id = db.add_invoice(invoice_data, "Manual", g.user_id)
        if new_id: return jsonify({"ok": True, "id": new_id}), 201
        else: return jsonify({"ok": False, "error": "No se pudo guardar"}), 500
@app.route('/api/invoice/<int:invoice_id>', methods=['GET', 'DELETE'])
@check_token
def handle_single_invoice(invoice_id):
    if request.method == 'GET':
        details = db.get_invoice_details(invoice_id, g.user_id)
        if details: return jsonify({"ok": True, "invoice": details})
        else: return jsonify({"ok": False, "error": "No encontrada"}), 404
    if request.method == 'DELETE':
        success = db.delete_invoice(invoice_id, g.user_id)
        if success: return jsonify({"ok": True, "message": "Factura borrada"})
        else: return jsonify({"ok": False, "error": "No se pudo borrar"}), 404

# --- AÑADIDO: Nuevo endpoint para guardar las notas de una factura ---
@app.route('/api/invoice/<int:invoice_id>/notes', methods=['PUT'])
@check_token
def update_notes(invoice_id):
    try:
        data = request.get_json()
        if 'notas' not in data:
            return jsonify({"ok": False, "error": "Falta el campo 'notas'"}), 400
        
        success = db.update_invoice_notes(invoice_id, g.user_id, data['notas'])
        if success:
            return jsonify({"ok": True, "message": "Notas actualizadas"})
        else:
            return jsonify({"ok": False, "error": "No se pudo actualizar o la factura no existe"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500


# --- MODIFICADO: Renombrado de /api/ask a /api/ai/query y lógica mejorada ---
@app.route('/api/ai/query', methods=['POST'])
@check_token
def ai_query():
    try:
        query_data = request.get_json()
        if not query_data or 'query' not in query_data:
            return jsonify({"ok": False, "error": "No se ha proporcionado ninguna pregunta."}), 400
            
        user_query = query_data['query']
        all_invoices = db.get_all_invoices_with_details(g.user_id)
        
        if not all_invoices:
            return jsonify({"ok": True, "answer": "No tienes ninguna factura registrada para que pueda responder."})
            
        # Convertimos los datos a un formato de texto más simple para el prompt
        invoices_context = json.dumps(all_invoices, indent=2, ensure_ascii=False, default=str)
        
        prompt_contextual = f"""
        Actúa como un asistente financiero experto y servicial.
        A continuación, te proporciono todas las facturas del usuario en formato JSON.
        Tu única fuente de información son estos datos. No inventes nada.
        
        DATOS DE FACTURAS:
        ```json
        {invoices_context}
        ```
        
        PREGUNTA DEL USUARIO:
        "{user_query}"
        
        Responde a la pregunta de forma clara y concisa. Si la pregunta implica un cálculo, realízalo.
        Por ejemplo, si pregunta 'cuál es la más cara', busca el 'total' más alto y menciona el emisor y el total.
        Si la pregunta no se puede responder con los datos, dilo amablemente.
        """
        
        response = gemini_model.generate_content(prompt_contextual)
        return jsonify({"ok": True, "answer": response.text})
        
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error interno: {str(e)}"}), 500

# (Search y main sin cambios)
@app.route('/api/search', methods=['POST'])
@check_token
def search():
    # ...
    pass
if __name__ == '__main__':
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))