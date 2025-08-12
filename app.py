# app.py - VERSIÓN CON ASISTENTE DE IA GEMINI

import os
import json
import io
from flask import Flask, request, jsonify
from PIL import Image
import google.generativeai as genai
import database as db

try:
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("No se encontró la GOOGLE_API_KEY en las variables de entorno.")
    genai.configure(api_key=api_key)
except Exception as e:
    print(f"Error CRÍTICO al configurar Gemini: {e}")

app = Flask(__name__)
db.DATABASE_FILE = '/tmp/facturas.db'
db.init_db()

gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')

# ... (El prompt y la ruta /api/process_invoice no cambian)
prompt_plantilla_factura = """
Actúa como un experto contable...
... (resto del prompt sin cambios)
"""

@app.route('/api/process_invoice', methods=['POST'])
def process_invoice():
    # ... (código de la función sin cambios)
    if not request.data:
        return jsonify({"error": "No se ha enviado ninguna imagen"}), 400
    try:
        image_bytes = io.BytesIO(request.data)
        img = Image.open(image_bytes)
        response = gemini_model.generate_content([prompt_plantilla_factura, img])
        json_text = response.text.replace('```json', '').replace('```', '').strip()
        extracted_data = json.loads(json_text)
        db.add_invoice(extracted_data, "gemini-1.5-flash-latest (real)")
        return jsonify({"ok": True, "data": extracted_data})
    except Exception as e:
        return jsonify({"error": f"Error del servidor de IA: {e}"}), 500


@app.route('/api/invoices', methods=['GET', 'POST'])
def handle_invoices():
    # ... (código de la función sin cambios)
    if request.method == 'GET':
        invoices = db.get_all_invoices()
        return jsonify({"ok": True, "invoices": invoices})
    if request.method == 'POST':
        invoice_data = request.get_json()
        if not invoice_data or not invoice_data.get('emisor'):
            return jsonify({"ok": False, "error": "Datos inválidos"}), 400
        new_id = db.add_invoice(invoice_data, "Manual")
        return jsonify({"ok": True, "id": new_id}), 201

@app.route('/api/invoice/<int:invoice_id>', methods=['GET'])
def get_invoice(invoice_id):
    # ... (código de la función sin cambios)
    details = db.get_invoice_details(invoice_id)
    if details:
        return jsonify({"ok": True, "invoice": details})
    else:
        return jsonify({"ok": False, "error": "Factura no encontrada"}), 404

# --- >>> NUEVA RUTA PARA EL ASISTENTE CONVERSACIONAL <<< ---
@app.route('/api/ask', methods=['POST'])
def ask_assistant():
    query_data = request.get_json()
    if not query_data or 'query' not in query_data:
        return jsonify({"ok": False, "error": "No se ha proporcionado ninguna pregunta."}), 400

    user_query = query_data['query']
    
    # 1. Obtener todas las facturas con sus detalles para dar contexto a Gemini
    all_invoices = db.get_all_invoices_with_details()

    if not all_invoices:
        return jsonify({"ok": True, "answer": "No tienes ninguna factura registrada todavía. Añade algunas para poder analizarlas."})

    # 2. Construir el prompt para Gemini
    try:
        # Convertimos las facturas a un string JSON para inyectarlo en el prompt
        invoices_context = json.dumps(all_invoices, indent=2, ensure_ascii=False)

        prompt_contextual = f"""
        Actúa como un asistente experto en contabilidad y finanzas personales.
        A continuación, te proporciono una lista completa de las facturas de un usuario en formato JSON.
        Tu tarea es responder a la pregunta del usuario basándote únicamente en estos datos.

        DATOS DE LAS FACTURAS:
        ```json
        {invoices_context}
        ```

        PREGUNTA DEL USUARIO:
        "{user_query}"

        Proporciona una respuesta clara, concisa y directa. Si la pregunta no se puede responder con los datos proporcionados, indica amablemente que no tienes esa información. No inventes datos.
        """

        # 3. Enviar la consulta a Gemini
        response = gemini_model.generate_content(prompt_contextual)
        
        # 4. Devolver la respuesta de Gemini al frontend
        return jsonify({"ok": True, "answer": response.text})

    except Exception as e:
        print(f"Error al contactar con Gemini: {e}")
        return jsonify({"ok": False, "error": f"Error del servidor de IA: {e}"}), 500