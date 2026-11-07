
from flask import Flask, request, jsonify
import json
import re
import os
import mysql.connector
import traceback
from openai import OpenAI

app = Flask(__name__)

# ------------------------
# OpenRouter API
# ------------------------

OPENROUTER_API_KEY = (
    os.getenv("OPENROUTER_API_KEY") or ""
).strip()

OPENROUTER_API_BASE = (
    os.getenv(
        "OPENROUTER_API_BASE",
        "https://openrouter.ai/api/v1"
    ) or ""
).strip().rstrip("/")

if not OPENROUTER_API_KEY:
    raise RuntimeError(
        "OPENROUTER_API_KEY is missing in Railway Variables."
    )

client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_API_BASE
)

MODEL_NAME = "openai/gpt-4o-mini"

print("OpenRouter configuration loaded.")
print("OpenRouter API key detected:", bool(OPENROUTER_API_KEY))
print("OpenRouter API key length:", len(OPENROUTER_API_KEY))
print("OpenRouter model:", MODEL_NAME)


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
# OpenRouter AI Helper
# ------------------------

def generate_ai_answer(messages, temperature=0.3):

    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is missing.")

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        temperature=temperature
    )

    answer = response.choices[0].message.content

    if not answer:
        raise ValueError("The AI returned an empty response.")

    return answer.strip()


# ------------------------
# Occupancy Type Helper
# ------------------------

def get_occupancy_type(rooms):

    preferences = set()

    for room in rooms:

        preference = str(
            room.get("genderPreference") or ""
        ).strip()

        if preference:
            preferences.add(preference.lower())

    if not preferences:
        return "Not specified"

    if len(preferences) > 1:
        return "Mixed"

    preference = next(iter(preferences))

    if "female" in preference:
        return "Female only"

    if "male" in preference:
        return "Male only"

    if "mixed" in preference or "both" in preference:
        return "Mixed"

    return next(
        (
            room.get("genderPreference")
            for room in rooms
            if room.get("genderPreference")
        ),
        "Not specified"
    )


# ------------------------
# Dormitories Recommendation
# ------------------------

@app.route("/ask-ai/dormitories", methods=["POST"])
def ask_ai_dormitories():

    conn = None
    cursor = None

    try:

        data = request.get_json(silent=True) or {}

        if "question" not in data:
            return jsonify({
                "message": "No question provided",
                "result": [],
                "recommendations": []
            }), 400

        user_question = str(
            data.get("question", "")
        ).lower().strip()

        if not user_question:
            return jsonify({
                "message": "Please enter a question.",
                "result": [],
                "recommendations": []
            }), 400

        # Keywords
        room_keywords = ["room", "bedspace", "unit"]
        greetings = ["hello", "hi", "hey"]

        # Greeting
        if any(
            greet in user_question.split()
            for greet in greetings
        ) and not any(
            word in user_question
            for word in room_keywords
        ):

            return jsonify({
                "message": (
                    "Hello! I am your DormDash assistant. "
                    "Ask about available dormitories or rooms "
                    "in Surigao City."
                ),
                "result": [],
                "recommendations": []
            })

        # Only allow Surigao City
        mentioned_cities = re.findall(
            r"\b(?:cebu|mandaue|lapu-lapu|davao|manila)\b",
            user_question
        )

        if mentioned_cities:

            return jsonify({
                "message": (
                    "Sorry, DormDash currently only has "
                    "dormitories in Surigao City."
                ),
                "result": [],
                "recommendations": []
            })

        # Price filter
        price_matches = re.findall(
            r"\$?(\d{3,5})",
            user_question
        )

        price_filter_min = None
        price_filter_max = None

        if len(price_matches) >= 2:

            price_filter_min = float(price_matches[0])
            price_filter_max = float(price_matches[1])

        elif len(price_matches) == 1:

            price_filter_max = float(price_matches[0])

        # Database connection
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Fetch dormitories and landlord information
        cursor.execute("""
            SELECT
                d.dormID,
                d.dormName,
                d.address,
                d.description,
                d.fklandlordID,
                l.firstname AS landlordFirstName,
                l.lastname AS landlordLastName,
                l.email AS landlordEmail,
                l.phonenumber AS landlordPhone

            FROM dorms d

            LEFT JOIN landlords l
                ON d.fklandlordID = l.landlordID

            WHERE LOWER(d.address) LIKE %s

        """, ("%surigao city%",))

        dorms = cursor.fetchall()

        cursor.close()
        cursor = None

        conn.close()
        conn = None

        if not dorms:

            return jsonify({
                "message": "No dormitories available in Surigao City.",
                "result": [],
                "recommendations": []
            })

        dorms_for_ui = []

        for dorm in dorms:

            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            # Fetch rooms and features
            cursor.execute("""
                SELECT
                    ro.roomID,
                    ro.roomNumber,
                    ro.roomType,
                    ro.availability,
                    ro.price,
                    ro.furnishing_status,
                    ro.genderPreference,
                    ro.fkdormID,
                    ro.fklandlordID,

                    GROUP_CONCAT(
                        DISTINCT rf.featureName
                    ) AS features

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
            cursor = None

            conn.close()
            conn = None

            formatted_rooms = []

            for room in rooms:

                try:
                    price = float(room.get("price") or 0)

                except (ValueError, TypeError):
                    price = 0

                # Minimum price filter
                if (
                    price_filter_min is not None
                    and price < price_filter_min
                ):
                    continue

                # Maximum price filter
                if (
                    price_filter_max is not None
                    and price > price_filter_max
                ):
                    continue

                features = room.get("features") or ""

                formatted_rooms.append({

                    "roomID": room["roomID"],
                    "roomNumber": room["roomNumber"],
                    "type": room["roomType"],
                    "price": price,
                    "availability": room["availability"],

                    "genderPreference": room.get(
                        "genderPreference"
                    ),

                    "features": (
                        features.split(",") if features else []
                    )

                })

            if not formatted_rooms:
                continue

            # Actual occupancy type based on room preferences
            occupancy_type = get_occupancy_type(rooms)

            dorms_for_ui.append({

                "dormID": dorm["dormID"],
                "dormName": dorm["dormName"],
                "address": dorm["address"],

                "occupancyType": occupancy_type,

                "amenities": "",
                "rules": [],
                "rooms": formatted_rooms,
                "dormimages": {},

                "fklandlordID": dorm["fklandlordID"],

                "landlord": {

                    "name": (
                        f"{dorm['landlordFirstName'] or ''} "
                        f"{dorm['landlordLastName'] or ''}"
                    ).strip(),

                    "email": dorm.get("landlordEmail") or "N/A",
                    "phone": dorm.get("landlordPhone") or "N/A"

                }

            })

        if not dorms_for_ui:

            return jsonify({
                "message": (
                    "No available rooms in Surigao City "
                    "within the specified price range."
                ),
                "result": [],
                "recommendations": []
            })

        # AI Summary
        ai_message = None

        try:

            full_prompt = f"""
You are a friendly dorm recommendation assistant.

Generate a short summary for the user based on
these dormitories and rooms located ONLY in Surigao City.

DORMITORY INFORMATION:

{json.dumps(dorms_for_ui, default=str)}

IMPORTANT:
- Recommend only the provided dormitories.
- Do not invent prices or room availability.
- Mention relevant room prices and features.
- Mention occupancy type when relevant.
- Keep the answer short and friendly.
- Do not recommend dormitories outside Surigao City.
"""

            ai_message = generate_ai_answer([

                {
                    "role": "system",
                    "content": (
                        "You help users find dorm rooms "
                        "in Surigao City only."
                    )
                },

                {
                    "role": "user",
                    "content": full_prompt
                }

            ])

        except Exception as e:

            print("OpenRouter API error:", str(e))
            traceback.print_exc()

            ai_message = (
                "AI recommendations unavailable. "
                "Showing dormitories from the database only."
            )

        return jsonify({

            "message": ai_message,
            "result": dorms_for_ui,
            "recommendations": dorms_for_ui

        })

    except Exception as e:

        traceback.print_exc()

        return jsonify({

            "message": str(e),
            "result": [],
            "recommendations": []

        }), 500

    finally:

        if cursor:
            cursor.close()

        if conn:
            conn.close()


# ------------------------
# Individual Dorm AI
# ------------------------

@app.route("/ask-ai/<int:dorm_id>", methods=["POST"])
def ask_ai(dorm_id):

    conn = None
    cursor = None

    try:

        # Connect to database
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Fetch dorm + landlord + amenities + rules
        cursor.execute("""
            SELECT
                d.dormID,
                d.dormName,
                d.address,
                d.description,
                d.latitude,
                d.longitude,

                GROUP_CONCAT(
                    DISTINCT a.aminityName
                ) AS amenities,

                GROUP_CONCAT(
                    DISTINCT r.rulesName
                ) AS rules,

                l.firstname AS landlordFirstName,
                l.lastname AS landlordLastName,
                l.email AS landlordEmail,
                l.phonenumber AS landlordPhone

            FROM dorms d

            LEFT JOIN landlords l
                ON d.fklandlordID = l.landlordID

            LEFT JOIN amenitydorm ad
                ON d.dormID = ad.fkdormID

            LEFT JOIN amenities a
                ON ad.fkaminityID = a.id

            LEFT JOIN rulesandpolicydorm rd
                ON d.dormID = rd.fkdormID

            LEFT JOIN rulesandpolicies r
                ON rd.fkruleID = r.id

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

        # Validate Surigao location
        address = (
            dorm.get("address") or ""
        ).lower()

        if not (
            "surigao city" in address
            or "surigao del norte" in address
        ):

            return jsonify({
                "success": False,
                "error": (
                    "This dormitory is not located in Surigao City."
                )
            }), 404

        # Fetch rooms
        cursor.execute("""
            SELECT
                ro.roomNumber,
                ro.roomType,
                ro.availability,
                ro.price,
                ro.furnishing_status,
                ro.genderPreference,

                GROUP_CONCAT(
                    DISTINCT rf.featureName
                ) AS features

            FROM rooms ro

            LEFT JOIN room_features_rooms rfr
                ON ro.roomID = rfr.fkroomID

            LEFT JOIN roomfeatures rf
                ON rfr.fkfeatureID = rf.id

            WHERE ro.fkdormID = %s

            GROUP BY ro.roomID

        """, (dorm_id,))

        rooms = cursor.fetchall()

        # Close database connection
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

        question = str(question).strip()

        if not question:

            return jsonify({
                "success": False,
                "error": "Please enter a message."
            }), 400

        # Normalize question
        normalized_question = question.lower().strip()
        normalized_question = normalized_question.rstrip("!.,? ")

        # Greeting responses
        greeting_responses = {

            "hello": (
                "Hello! 👋 Welcome to DormDash! "
                "I'm your AI dormitory assistant. "
                "How can I help you today?"
            ),

            "hi": (
                f"Hi there! 😊 Welcome to DormDash! "
                f"What would you like to know about {dorm['dormName']}?"
            ),

            "hii": (
                "Hii! 👋 Nice to meet you! "
                "How can I assist you with your dormitory needs today?"
            ),

            "hiii": (
                "Hiii! 😊 Welcome to DormDash! "
                "Feel free to ask me anything about this dorm."
            ),

            "hey": (
                "Hey there! 👋 I'm your DormDash AI assistant. "
                "What can I help you with?"
            ),

            "heyy": (
                "Heyyy! 😊 Welcome to DormDash! "
                "What would you like to know?"
            ),

            "good morning": (
                "Good morning! ☀️ Welcome to DormDash! "
                "How can I help you today?"
            ),

            "good afternoon": (
                "Good afternoon! 😊 Welcome to DormDash! "
                "What can I help you with?"
            ),

            "good evening": (
                "Good evening! 🌙 Welcome to DormDash! "
                "How may I assist you today?"
            ),

            "good day": (
                "Good day! 👋 Welcome to DormDash! "
                "How can I assist you?"
            ),

            "hola": (
                "Hola! 👋 Welcome to DormDash! "
                "How can I help you today?"
            ),

            "sup": (
                "Hey! 😎 Welcome to DormDash! "
                "What would you like to know about this dorm?"
            )

        }

        # Return greeting without calling AI
        if normalized_question in greeting_responses:

            return jsonify({

                "success": True,
                "answer": greeting_responses[normalized_question],
                "dorm": dorm,
                "rooms": rooms

            }), 200

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

        # AI Prompt
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
7. Be friendly and conversational.
8. If asked for landlord contact details, provide the available landlord name, email, and phone number exactly as supplied.
"""

        # Generate AI answer using OpenRouter
        try:

            ai_answer = generate_ai_answer([

                {
                    "role": "system",
                    "content": (
                        "You are DormDash's AI assistant "
                        "for Surigao City dormitories."
                    )
                },

                {
                    "role": "user",
                    "content": prompt
                }

            ])

        except Exception as e:

            print("OpenRouter API error:", str(e))
            traceback.print_exc()

            ai_answer = (
                "I'm sorry, my AI service is temporarily unavailable. "
                "However, I can still help you with the available "
                "dormitory information."
            )

        # Return response
        return jsonify({

            "success": True,
            "answer": ai_answer,
            "dorm": dorm,
            "rooms": rooms

        }), 200

    except Exception as e:

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


# ------------------------
# Health Check
# ------------------------

@app.route("/", methods=["GET"])
def health_check():

    return jsonify({

        "success": True,
        "message": "DormDash AI service is running."

    }), 200


# ------------------------
# Run Flask
# ------------------------

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )

