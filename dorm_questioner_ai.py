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
            return jsonify({
                "message": "No question provided",
                "result": [],
                "recommendations": []
            }), 400

        user_question = data.get("question", "").lower().strip()

        # Keywords & cities
        room_keywords = ["room", "bedspace", "unit"]
        greetings = ["hello", "hi", "hey"]
        allowed_cities = ["surigao city"]

        # Detect requested city
        user_cities = [city for city in allowed_cities if city in user_question]

        # Greeting
        if any(greet in user_question for greet in greetings) and not any(
                word in user_question for word in room_keywords
        ):
            return jsonify({
                "message": "Hello! I am your DormDash assistant. Ask about available dormitories or rooms in Surigao City.",
                "result": [],
                "recommendations": []
            })

        # Block unsupported cities
        blocked_cities = [
            "surigao",
            "surigao city"
        ]

        if any(city in user_question for city in blocked_cities):
            return jsonify({
                "message": "Sorry, DormDash currently only has dormitories in Surigao City.",
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
            WHERE LOWER(d.address) LIKE %s
        """, ("%surigao city%",))

        dorms = cursor.fetchall()
        cursor.close()
        conn.close()

        if not dorms:
            return jsonify({
                "message": "No dormitories available in Surigao City.",
                "result": [],
                "recommendations": []
            })

        dorms_for_ui = []

        for dorm in dorms:

            # Rooms for this dorm
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            cursor.execute("""
                SELECT ro.roomID, ro.roomNumber, ro.roomType,
                       ro.availability, ro.price,
                       ro.furnishing_status, ro.genderPreference,
                       ro.fkdormID, ro.fklandlordID,
                       GROUP_CONCAT(DISTINCT rf.featureName) AS features
                FROM rooms ro
                LEFT JOIN room_features_rooms rfr
                    ON ro.roomID = rfr.fkroomID
                LEFT JOIN roomfeatures rf
                    ON rfr.fkfeatureID = rf.id
                WHERE ro.fkdormID = %s
                GROUP BY ro.roomID
            """, (dorm["dormID"],))

            rooms = cursor.fetchall()
            cursor.close()
            conn.close()

            formatted_rooms = []

            for room in rooms:
                try:
                    price = float(room.get("price") or 0)
                except (ValueError, TypeError):
                    price = 0

                # Apply price filter
                if price_filter_min is not None and price < price_filter_min:
                    continue

                if price_filter_max is not None and price > price_filter_max:
                    continue

                features = room.get("features") or ""

                formatted_rooms.append({
                    "roomID": room["roomID"],
                    "roomNumber": room["roomNumber"],
                    "type": room["roomType"],
                    "price": price,
                    "availability": room["availability"],
                    "features": features.split(",") if features else []
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
                    "email": dorm.get("landlordEmail") or "N/A",
                    "phone": dorm.get("landlordPhone") or "N/A"
                }
            })

        if not dorms_for_ui:
            return jsonify({
                "message": "No available rooms in Surigao City within the specified price range.",
                "result": [],
                "recommendations": []
            })

        # Optional AI summary
        ai_message = None

        try:
            full_prompt = f"""
You are a friendly dorm recommendation assistant.
Generate a short text summary for the user based on these
dormitories and rooms located ONLY in Surigao City:

{json.dumps(dorms_for_ui, default=str)}

Do not recommend dormitories outside Surigao City.
"""

            response = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "You help users find dorm rooms in Surigao City only."
                    },
                    {
                        "role": "user",
                        "content": full_prompt
                    }
                ],
                temperature=0.3
            )

            ai_message = response["choices"][0]["message"]["content"]

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

        return jsonify({
            "message": str(e),
            "result": [],
            "recommendations": []
        }), 500



@app.route("/ask-ai/<int:dorm_id>", methods=["POST"])
def ask_ai(dorm_id):
    conn = None
    cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Fetch dorm + landlord + amenities + rules
        cursor.execute("""
            SELECT d.dormID, d.dormName, d.address, d.description,
                   d.latitude, d.longitude,
                   GROUP_CONCAT(DISTINCT a.aminityName) AS amenities,
                   GROUP_CONCAT(DISTINCT r.rulesName) AS rules,
                   l.firstname AS landlordFirstName,
                   l.lastname AS landlordLastName,
                   l.email AS landlordEmail,
                   l.phonenumber AS landlordPhone
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

        # Check if dorm exists
        if not dorm:
            return jsonify({
                "success": False,
                "error": "Dormitory not found."
            }), 404

        # Validate Surigao City address
        address = (dorm.get("address") or "").lower()

        if "surigao city" not in address:
            return jsonify({
                "success": False,
                "error": "This dormitory is not located in Surigao City."
            }), 404

        # Fetch rooms
        cursor.execute("""
            SELECT ro.roomNumber, ro.roomType, ro.availability,
                   ro.price, ro.furnishing_status,
                   ro.genderPreference,
                   GROUP_CONCAT(DISTINCT rf.featureName) AS features
            FROM rooms ro
            LEFT JOIN room_features_rooms rfr
                ON ro.roomID = rfr.fkroomID
            LEFT JOIN roomfeatures rf
                ON rfr.fkfeatureID = rf.id
            WHERE ro.fkdormID = %s
            GROUP BY ro.roomID
        """, (dorm_id,))

        rooms = cursor.fetchall()

        # Close database connection after fetching data
        cursor.close()
        cursor = None

        conn.close()
        conn = None

        # Get tenant question
        data = request.get_json(silent=True) or {}

        question = data.get(
            "question",
            f"Tell me about {dorm['dormName']}."
        )

        # Landlord information
        landlord_info = (
            f"{dorm.get('landlordFirstName') or 'N/A'} "
            f"{dorm.get('landlordLastName') or ''}, "
            f"Email: {dorm.get('landlordEmail') or 'N/A'}, "
            f"Phone: {dorm.get('landlordPhone') or 'N/A'}"
        )

        # Dormitory information
        dorm_info = f"""
Dormitory Name: {dorm['dormName']}
Location: {dorm['address']}
Description: {dorm.get('description') or 'Not provided'}
Amenities: {dorm.get('amenities') or 'None'}
Rules: {dorm.get('rules') or 'None'}
Landlord: {landlord_info}
"""

        # Room information
        rooms_info = ""

        for room in rooms:
            features = room.get("features") or "None"

            rooms_info += f"""
- Room {room['roomNumber']}
  Type: {room['roomType']}
  Price: {room['price']}
  Availability: {room['availability']}
  Furnishing: {room['furnishing_status']}
  Gender Preference: {room['genderPreference']}
  Features: {features}
"""

        # AI prompt
        prompt = f"""
You are DormDash's friendly AI dormitory assistant.

You are assisting tenants looking for dormitories in Surigao City.

Use ONLY the information provided below.

DORMITORY INFORMATION:
{dorm_info}

ROOM INFORMATION:
{rooms_info}

TENANT QUESTION:
{question}

IMPORTANT RULES:
1. Answer based only on the provided database information.
2. Do not invent prices, amenities, rules, or room availability.
3. If information is missing, politely say it is not provided.
4. Keep answers short, clear, and friendly.
5. Do not recommend dormitories outside Surigao City.
6. If the question is unrelated to this dormitory, politely redirect the tenant to dorm-related questions.
"""

        # Generate AI answer
        try:
            response = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "You are DormDash's AI assistant for Surigao City dormitories."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.3
            )

            ai_answer = response["choices"][0]["message"]["content"]

        except Exception as e:
            print("OpenAI API error:", str(e))

            ai_answer = (
                "AI recommendations unavailable. "
                "Showing dorm information only."
            )

        # Return response
        return jsonify({
            "success": True,
            "answer": ai_answer,
            "dorm": dorm,
            "rooms": rooms
        }), 200

    except Exception as e:
        import traceback
        traceback.print_exc()

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

    finally:
        if cursor:
            cursor.close()

        if conn:
            conn.close()



if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

