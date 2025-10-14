from flask import Flask, request, jsonify
import json
import re
import openai
import os
import mysql.connector

app = Flask(__name__)

# ------------------------
# OpenRouter / OpenAI API
# ------------------------
openai.api_key = os.getenv("OPENROUTER_API_KEY")
openai.api_base = os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
openai.api_type = "open_ai"
openai.api_version = None

# ------------------------
# Database Connection
# ------------------------
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", 3306)),
        user=os.getenv("DB_USERNAME"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_DATABASE")
    )

# ------------------------
# Dormitories Endpoint
# ------------------------
@app.route("/ask-ai/dormitories", methods=["POST"])
def ask_ai_dormitories():
    try:
        data = request.get_json()
        if not data or "question" not in data:
            return jsonify({"message": "No question provided", "result": [], "recommendations": []}), 400

        user_question = data.get("question", "").lower().strip()

        # Keywords & cities
        room_keywords = ["room", "bedspace", "unit"]
        greetings = ["hello", "hi", "hey"]
        allowed_cities = ["lapu-lapu", "mandaue"]
        blocked_cities = ["liloan", "cebu", "others"]

        # Detect requested cities
        user_cities = [city for city in allowed_cities if city in user_question]

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
                "message": "Sorry, DormHub currently only has dorms in Lapu-Lapu or Mandaue.",
                "result": [],
                "recommendations": []
            })

        # Price filter
        price_matches = re.findall(r"\$?(\d{3,5})", user_question)
        price_filter_min, price_filter_max = None, None
        if len(price_matches) >= 2:
            price_filter_min = float(price_matches[0])
            price_filter_max = float(price_matches[1])
        elif len(price_matches) == 1:
            price_filter_max = float(price_matches[0])

        # Connect to DB
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
        conn.close()

        if not dorms:
            return jsonify({"message": "No dorms available", "result": [], "recommendations": []})

        dorms_for_ui = []

        for dorm in dorms:
            # Only include dorms in requested cities
            if user_cities:
                address_lower = dorm["address"].lower()
                if not any(city in address_lower for city in user_cities):
                    continue

            # Rooms for this dorm
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT ro.roomID, ro.roomNumber, ro.roomType, ro.availability, ro.price,
                       ro.furnishing_status, ro.genderPreference, ro.fkdormID, ro.fklandlordID,
                       GROUP_CONCAT(DISTINCT rf.featureName) AS features
                FROM rooms ro
                LEFT JOIN room_features_rooms rfr ON ro.roomID = rfr.fkroomID
                LEFT JOIN roomfeatures rf ON rfr.fkfeatureID = rf.id
                WHERE ro.fkdormID = %s
                GROUP BY ro.roomID
            """, (dorm["dormID"],))
            rooms = cursor.fetchall()
            conn.close()

            formatted_rooms = []
            for room in rooms:
                try:
                    price = float(room.get("price") or 0)
                except (ValueError, TypeError):
                    price = 0
                if price_filter_min is not None and price < price_filter_min:
                    continue
                if price_filter_max is not None and price > price_filter_max:
                    continue
                features = room.get("features") or ""
                if isinstance(features, list):
                    features = ",".join(features)
                formatted_rooms.append({
                    "roomID": room["roomID"],
                    "roomNumber": room["roomNumber"],
                    "type": room["roomType"],
                    "price": price,
                    "availability": room["availability"],
                    "features": features.split(',') if features else []
                })

            if not formatted_rooms:
                continue

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
                    "email": dorm.get('landlordEmail', 'N/A'),
                    "phone": dorm.get('landlordPhone', 'N/A')
                }
            })

        if not dorms_for_ui:
            return jsonify({
                "message": f"No available rooms in {', '.join(user_cities).title() if user_cities else 'Lapu-Lapu or Mandaue'} within the specified price range.",
                "result": [],
                "recommendations": []
            })

        # Optional AI summary
        ai_message = None
        try:
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
        except Exception as e:
            print("❌ OpenAI API error:", e)
            ai_message = "AI recommendations unavailable. Showing dorms from database only."

        return jsonify({
            "message": ai_message,
            "result": dorms_for_ui,
            "recommendations": dorms_for_ui
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"message": str(e), "result": [], "recommendations": []}), 500

# ------------------------
# Single Dorm Endpoint
# ------------------------
@app.route("/ask-ai/<int:dorm_id>", methods=["POST"])
def ask_ai(dorm_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Fetch dorm + landlord + amenities + rules
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

        # Fetch rooms
        cursor.execute("""
            SELECT ro.roomNumber, ro.roomType, ro.availability, ro.price,
                   ro.furnishing_status, ro.genderPreference,
                   GROUP_CONCAT(DISTINCT rf.featureName) as features
            FROM rooms ro
            LEFT JOIN room_features_rooms rfr ON ro.roomID = rfr.fkroomID
            LEFT JOIN roomfeatures rf ON rfr.fkfeatureID = rf.id
            WHERE ro.fkdormID = %s
            GROUP BY ro.roomID
        """, (dorm_id,))
        rooms = cursor.fetchall()
        conn.close()

        data = request.get_json()
        question = data.get("question", f"Tell me about {dorm['dormName']}.")

        landlord_info = f"{dorm['landlordFirstName']} {dorm['landlordLastName']}, Email: {dorm.get('landlordEmail', 'N/A')}, Phone: {dorm.get('landlordPhone', 'N/A')}"
        dorm_info = f"Name: {dorm['dormName']}\nAddress: {dorm['address']}\nDescription: {dorm['description']}\nAmenities: {dorm.get('amenities','None')}\nRules: {dorm.get('rules','None')}\nLandlord: {landlord_info}"

        rooms_info = ""
        for room in rooms:
            features = room.get("features") or "None"
            rooms_info += f"- Room {room['roomNumber']}, Type: {room['roomType']}, Price: {room['price']}, Availability: {room['availability']}, Furnishing: {room['furnishing_status']}, Gender: {room['genderPreference']}, Features: {features}\n"

        prompt = f"{dorm_info}\nRooms:\n{rooms_info}\n\nTenant Question: {question}"

        ai_answer = None
        try:
            response = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are an assistant that summarizes a single dorm and its rooms for tenants."},
                    {"role": "user", "content": prompt}
                ]
            )
            ai_answer = response['choices'][0]['message']['content']
        except Exception as e:
            print("❌ OpenAI API error:", e)
            ai_answer = "AI recommendations unavailable. Showing dorm info only."

        return jsonify({
            "answer": ai_answer,
            "dorm": dorm,
            "rooms": rooms
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ------------------------
# Run on Railway
# ------------------------
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

