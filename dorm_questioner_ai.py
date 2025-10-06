from flask import Flask, request, jsonify
import json
import re
import openai
import os
import mysql.connector
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel



app = Flask(__name__)

# OpenRouter API key and base URL — use your own key or environment variables
openai.api_key = os.getenv("OPENROUTER_API_KEY") or "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"
openai.api_base = os.getenv("OPENROUTER_API_BASE") or "https://openrouter.ai/api/v1"
openai.api_type = "open_ai"
openai.api_version = None

# Connect to MySQL database
def get_db_connection():
    return mysql.connector.connect(
        host="136.113.184.28",
        user="dormhub",
        password="dormH@b2025",  # Put your DB password here
        database="capstonedormhub"
    )

# Clean code blocks or extra characters from OpenAI response
def extract_json_from_response(text):
    cleaned = re.sub(r"```json|```", "", text).strip()
    return cleaned

# Ask OpenRouter to extract filter values from a dorm-related question
def ask_openrouter(question):
    prompt = f"""
Extract filter values from this question: "{question}"

Available locations: Cebu City, Lapu-Lapu, Mandaue
Room types: studio, shared, single

Return ONLY valid JSON in this format:
{{
    "location": null or string,
    "type": null or string,
    "max_price": null or number,
    "lowest": true or false,
    "highest": true or false
}}
"""
    try:
        response = openai.ChatCompletion.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that extracts room search filters."},
                {"role": "user", "content": prompt}
            ]
        )
        content = response['choices'][0]['message']['content']
        clean_json_text = extract_json_from_response(content)
        filters = json.loads(clean_json_text)
        return filters
    except Exception as e:
        print("❌ Error from OpenRouter:", e)
        return None

# Query dorms from database based on filters extracted
@app.route("/ask-ai/dormitories", methods=["POST"])
def ask_ai_dormitories():
    try:
        data = request.get_json()
        user_question = data.get("question", "").lower().strip()

        # Keywords
        room_keywords = ["room", "bedspace", "unit"]
        greetings = ["hello", "hi", "hey"]
        allowed_cities = ["lapu-lapu", "mandaue"]
        blocked_cities = ["liloan", "cebu", "others"]

        # Greeting
        if any(greet in user_question for greet in greetings) and not any(word in user_question for word in room_keywords):
            return jsonify({
                "message": "Hello! I am your DormHub assistant. Ask about available dormitories or rooms in Lapu-Lapu or Mandaue.",
                "result": [],
                "recommendations": []
            })

        # Blocked city
        if any(city in user_question for city in blocked_cities):
            return jsonify({
                "message": "Sorry, DormHub currently only has dorms in Lapu-Lapu and Mandaue.",
                "result": [],
                "recommendations": []
            })

        # Price filter
        price_matches = re.findall(r"\$?(\d{3,5})", user_question)  # supports $ sign
        price_filter_min, price_filter_max = None, None
        if len(price_matches) >= 2:
            price_filter_min = float(price_matches[0])
            price_filter_max = float(price_matches[1])
        elif len(price_matches) == 1:
            price_filter_max = float(price_matches[0])

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Fetch dorms + landlord
        cursor.execute("""
            SELECT d.dormID, d.dormName, d.address, d.description,
                   d.fklandlordID,
                   l.firstname AS landlordFirstName,
                   l.lastname AS landlordLastName,
                   l.email AS landlordEmail,
                   l.phonenumber AS landlordPhone
            FROM dorms d
            LEFT JOIN landlords l ON d.fklandlordID = l.landlordID
        """)
        dorms = cursor.fetchall()
        if not dorms:
            return jsonify({"message": "No dorms available", "result": [], "recommendations": []})

        dorms_for_ui = []

        for dorm in dorms:
            # Skip if dorm not in allowed cities
            if not any(city in dorm["address"].lower() for city in allowed_cities):
                continue

            # Rooms query
            cursor.execute("""
                SELECT ro.roomID, ro.roomNumber, ro.roomType, ro.availability, ro.price,
                       ro.furnishing_status, ro.genderPreference, ro.fkdormID, ro.fklandlordID,
                       GROUP_CONCAT(DISTINCT rf.featureName) AS features
                FROM rooms ro
                LEFT JOIN roomfeaturesrooms rfr ON ro.roomID = rfr.fkroomID
                LEFT JOIN roomfeatures rf ON rfr.fkfeatureID = rf.id
                WHERE ro.fkdormID = %s
                GROUP BY ro.roomID
            """, (dorm["dormID"],))
            rooms = cursor.fetchall()

            formatted_rooms = []
            for room in rooms:
                try:
                    price = float(room["price"])
                except (ValueError, TypeError):
                    price = 0

                # Apply price filter
                if price_filter_min and price < price_filter_min:
                    continue
                if price_filter_max and price > price_filter_max:
                    continue

                features = room["features"]
                if features is None:
                    features = ""
                elif isinstance(features, list):
                    features = ",".join(features)

                formatted_rooms.append({
                    "roomID": room["roomID"],
                    "roomNumber": room["roomNumber"],
                    "type": room["roomType"],
                    "price": price,  # Frontend will display ₱
                    "availability": room["availability"],
                    "features": features.split(',') if features else []
                })

            if not formatted_rooms:
                continue  # skip dorm if no rooms available

            dorms_for_ui.append({
                "dormID": dorm["dormID"],
                "dormName": dorm["dormName"],
                "address": dorm["address"],
                "occupancyType": "Mixed",
                "amenities": "",
                "rules": [],
                "rooms": formatted_rooms,
                "dormimages": {},
                "fklandlordID": dorm["fklandlordID"],
                "landlord": {
                    "name": f"{dorm['landlordFirstName']} {dorm['landlordLastName']}",
                    "email": dorm['landlordEmail'],
                    "phone": dorm['landlordPhone']
                }
            })

        conn.close()

        if not dorms_for_ui:
            return jsonify({
                "message": "No available rooms in Lapu-Lapu or Mandaue within the specified price range.",
                "result": [],
                "recommendations": []
            })

        # AI message prompt
        full_prompt = f"""
You are a friendly dorm recommendation assistant.
Generate a short text summary for the user based on these dorms and rooms:
{json.dumps(dorms_for_ui, default=str)}
"""

        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You help users find dorm rooms."},
                {"role": "user", "content": full_prompt}
            ],
            temperature=0.3
        )

        ai_message = response['choices'][0]['message']['content']

        return jsonify({
            "message": ai_message,
            "result": dorms_for_ui,
            "recommendations": dorms_for_ui
        })

    except Exception as e:
        return jsonify({"message": str(e), "result": [], "recommendations": []}), 500
@app.route("/ask-ai/<int:dorm_id>", methods=["POST"])
def ask_ai(dorm_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # --- Fetch dorm info + landlord ---
        cursor.execute("""
            SELECT d.dormName, d.address, d.description,
                   d.latitude, d.longitude,
                   GROUP_CONCAT(DISTINCT a.aminityName) as amenities,
                   GROUP_CONCAT(DISTINCT r.rulesName) as rules,
                   l.firstname as landlordFirstName,
                   l.lastname as landlordLastName,
                   l.email as landlordEmail,
                   l.phonenumber as landlordPhone
            FROM dorms d
            LEFT JOIN landlords l ON d.fklandlordID = l.landlordID
            LEFT JOIN amenitydorm ad ON d.dormID = ad.fkdormID
            LEFT JOIN amenities a ON ad.fkaminityID = a.id
            LEFT JOIN rulesandpolicydorm rd ON d.dormID = rd.fkdormID
            LEFT JOIN rulesandpolicies r ON rd.fkruleID = r.id
            WHERE d.dormID = %s
            GROUP BY d.dormID
        """, (dorm_id,))
        dorm = cursor.fetchone()

        if not dorm:
            conn.close()
            return jsonify({"error": "Dorm not found"}), 404

        # --- Fetch rooms and features ---
        cursor.execute("""
            SELECT ro.roomNumber, ro.roomType, ro.availability, ro.price, ro.furnishing_status, ro.genderPreference,
                   GROUP_CONCAT(DISTINCT rf.featureName) as features
            FROM rooms ro
            LEFT JOIN roomfeaturesrooms rfr ON ro.roomID = rfr.fkroomID
            LEFT JOIN roomfeatures rf ON rfr.fkfeatureID = rf.id
            WHERE ro.fkdormID = %s
            GROUP BY ro.roomID
        """, (dorm_id,))
        rooms = cursor.fetchall()

        # --- Fetch dorm images ---
        cursor.execute("""
            SELECT mainImage, secondaryImage, thirdImage
            FROM dormimages
            WHERE fkdormID = %s
        """, (dorm_id,))
        images = cursor.fetchone()
        conn.close()

        # --- Get user question ---
        data = request.get_json()
        user_question = data.get("question", "Tell me something about this dorm.")

        # --- Format dorm + landlord info for GPT ---
        landlord_info = f"{dorm.get('landlordFirstName', '')} {dorm.get('landlordLastName', '')}, Email: {dorm.get('landlordEmail', 'N/A')}, Phone: {dorm.get('landlordPhone', 'N/A')}"

        dorm_info = f"""
Name: {dorm['dormName']}
Location: {dorm['address']}
Description: {dorm['description']}
Amenities: {dorm.get('amenities', 'None')}
Rules & Policies: {dorm.get('rules', 'None')}
Landlord: {landlord_info}
Images: {images if images else 'No images'}
"""

        rooms_info = ""
        for room in rooms:
            rooms_info += f"- Room {room['roomNumber']}, Type: {room['roomType']}, Availability: {room['availability']}, Price: {room['price']}, Furnishing: {room['furnishing_status']}, Gender Preference: {room['genderPreference']}, Features: {room.get('features', 'None')}\n"

        full_prompt = f"{dorm_info}\nRooms:\n{rooms_info}\n\nQuestion: {user_question}"

        # --- Call OpenAI GPT-4o-mini ---
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an assistant helping tenants learn about dormitories."},
                {"role": "user", "content": full_prompt}
            ]
        )

        ai_answer = response['choices'][0]['message']['content']

        return jsonify({
            "answer": ai_answer,
            "dorm": dorm,
            "rooms": rooms,
            "images": images
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)
