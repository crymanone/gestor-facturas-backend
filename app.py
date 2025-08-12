# app.py - VERSIÓN CON MEJORES LOGS DE DEPURACIÓN Y ROBUSTEZ

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

prompt_plantilla_factura = """
Actúa como un experto contable especializado en la extracción de datos de documentos.
Analiza la siguiente imagen de una factura o ticket.
Extrae los siguientes campos y devuelve la respuesta estrictamente en formato JSON, sin texto introductorio, explicaciones o marcado de código.

Los campos a extraer son:
- emisor (El nombre de la empresa o persona que emite la factura)
- cif (El identificador fiscal: CIF, NIF, VAT ID, etc.)
- fecha (La fecha de emisión del documento en formato DD/MM/AAAA)
- total (El importe total final pagado, como un número)
- base_imponible (El subtotal antes de impuestos, como un número)
- impuestos (Un objeto JSON con los diferentes tipos de impuesto y su valor. Ej: {"iva_21": 21.00, "otros_impuestos": 2.50})
- conceptos (Una lista de objetos, donde cada objeto contiene 'descripcion', 'cantidad' y 'precio_unitario')

Si un campo no se puede encontrar o no es aplicable, devuélvelo como `null`.
Si los conceptos son difíciles de desglosar, extrae al menos una descripción general como un único concepto.
"""

@app.route('/api/process_invoice', methods=['POST'])
def process_invoice():
    if not request.data:
        return jsonify({"ok": False, "error": "No se ha enviado ninguna imagen"}), 400
    try:
        image_bytes = io.BytesIO(request.data)
        img = Image.open(image_bytes)
        
        print("Enviando imagen a Gemini...")
        response = gemini_model.generate_content([prompt_plantilla_factura, img])
        
        # --- MEJORA DE LOGS: Imprimimos la respuesta de Gemini ---
        print("Respuesta de Gemini recibida:")
        print(response.text)
        
        json_text = response.text.replace('```json', '').replace('```', '').strip()
        extracted_data = json.loads(json_text)
        
        print("Datos extraídos (JSON):")
        print(json.dumps(extracted_data, indent=2, ensure_ascii=False))
        
        invoice_id = db.add_invoice(extracted_data, "gemini-1.5-flash-latest (real)")
        
        if invoice_id:
            return jsonify({"ok": True, "data": extracted_data})
        else:
            # Si add_invoice devuelve None, es que hubo un error al guardar
            return jsonify({"ok": False, "error": "Los datos extraídos no se pudieron guardar en la base de datos."}), 500

    except json.JSONDecodeError as e:
        print(f"Error de JSON: La respuesta de Gemini no era un JSON válido. {e}")
        return jsonify({"ok": False, "error": f"La IA devolvió un formato de datos incorrecto."}), 500
    except Exception as e:
        # MEJORA DE LOGS: Imprimimos el error específico
        print(f"Error INESPERADO en process_invoice: {e}")
        return jsonify({"ok": False, "error": f"Error del servidor de IA: {e}"}), 500

@app.route('/api/invoices', methods=['GET', 'POST'])
def handle_invoices():
    if request.method == 'GET':
        try:
            invoices = db.get_all_invoices()
            return jsonify({"ok": True, "invoices": invoices})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    if request.method == 'POST':
        try:
            invoice_data = request.get_json()
            if not invoice_data or not invoice_data.get('emisor'):
                return jsonify({"ok": False, "error": "Datos inválidos o emisor faltante"}), 400
            
            new_id = db.add_invoice(invoice_data, "Manual")
            if new_id:
                return jsonify({"ok": True, "id": new_id, "message": "Factura añadida"}), 201
            else:
                return jsonify({"ok": False, "error": "No se pudo guardar la factura"}), 500
        except Exception as e:
            return jsonify({"error": str(e)}), 400

@app.route('/api/invoice/<int:invoice_id>', methods=['GET'])
def get_invoice(invoice_id):
    try:
        details = db.get_invoice_details(invoice_id)
        if details:
            return jsonify({"ok": True, "invoice": details})
        else:
            return jsonify({"ok": False, "error": "Factura no encontrada"}), 404
    except Exception as e:
        return jsonify({"error": f"Error del servidor: {e}"}), 500

@app.route('/api/ask', methods=['POST'])
def ask_assistant():
    query_data = request.get_json()
    if not query_data or 'query' not in query_data:
        return jsonify({"ok": False, "error": "No se ha proporcionado ninguna pregunta."}), 400

    user_query = query_data['query']
    
    all_invoices = db.get_all_invoices_with_details()

    if not all_invoices:
        return jsonify({"ok": True, "answer": "No tienes ninguna factura registrada todavía. Añade algunas para poder analizarlas."})

    try:
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

        response = gemini_model.generate_content(prompt_contextual)
        
        return jsonify({"ok": True, "answer": response.text})

    except Exception as e:
        print(f"Error al contactar con Gemini: {e}")
        return jsonify({"ok": False, "error": f"Error del servidor de IA: {e}"}), 500