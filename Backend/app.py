import os
import math
import datetime
import jwt
import bcrypt
import requests as http_requests
from functools import wraps
from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient
from dotenv import load_dotenv
from bson import ObjectId

load_dotenv()

app = Flask(__name__)
CORS(app, origins=["http://localhost:5173"], supports_credentials=True)

# ── Config ──────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET_KEY", "fallback-secret")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

# ── MongoDB ─────────────────────────────────────────────
client = MongoClient(MONGO_URI)
db = client["homie_db"]
users_collection = db["users"]

# Create indexes
users_collection.create_index("email", unique=True)
users_collection.create_index([("location", "2dsphere")])


# ═══════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════

def generate_token(user_id):
    """Generate a JWT token valid for 7 days."""
    payload = {
        "user_id": str(user_id),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(days=7),
        "iat": datetime.datetime.utcnow(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def hash_password(password):
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_password(password, hashed):
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


def token_required(f):
    """Decorator to protect routes with JWT authentication."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

        if not token:
            return jsonify({"error": "Token is missing"}), 401

        try:
            data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            current_user = users_collection.find_one({"_id": ObjectId(data["user_id"])})
            if not current_user:
                return jsonify({"error": "User not found"}), 401
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token has expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401

        return f(current_user, *args, **kwargs)
    return decorated


def serialize_user(user):
    """Convert MongoDB user document to JSON-safe dict."""
    coords = None
    if user.get("location") and user["location"].get("coordinates"):
        coords = user["location"]["coordinates"]  # [lng, lat]

    return {
        "id": str(user["_id"]),
        "email": user.get("email"),
        "name": user.get("name"),
        "role": user.get("role"),
        "location_text": user.get("location_text", ""),
        "location": {"lng": coords[0], "lat": coords[1]} if coords else None,
        "specialization": user.get("specialization"),
        "price_per_hour": user.get("price_per_hour"),
        "rating": user.get("rating", 0),
        "reviews_count": user.get("reviews_count", 0),
        "is_verified": user.get("is_verified", False),
        "is_online": user.get("is_online", False),
        "created_at": user.get("created_at", "").isoformat() if user.get("created_at") else None,
    }


# ── Geocoding (OpenStreetMap Nominatim — free, no API key) ──
def geocode_address(address):
    """
    Convert a text address to lat/lng coordinates using
    OpenStreetMap's free Nominatim geocoding API.
    Returns (lat, lng) tuple or None if not found.
    """
    try:
        res = http_requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": "HOMIE-App/1.0"},
            timeout=5
        )
        data = res.json()
        if data and len(data) > 0:
            lat = float(data[0]["lat"])
            lng = float(data[0]["lon"])
            return (lat, lng)
    except Exception as e:
        print(f"Geocoding error for '{address}': {e}")
    return None


# ── AI Scoring Engine ───────────────────────────────────
def calculate_ai_score(distance_km, rating, reviews_count, is_verified, is_online):
    """
    AI-powered scoring algorithm to rank professionals.
    
    Factors and weights:
      - Proximity (40%):  Closer workers score higher
      - Rating (30%):     Higher-rated workers score higher
      - Trust (20%):      More reviews = more trustworthy (log scale)
      - Availability (10%): Online & verified workers get a bonus
    
    Returns a score from 0 to 100.
    """
    # Proximity score: inverse distance, max out at 40 points
    # At 0km = 40pts, at 5km = 20pts, at 20km = 5pts, at 50km+ ≈ 0pts
    if distance_km <= 0:
        proximity_score = 40.0
    else:
        proximity_score = 40.0 * (1.0 / (1.0 + distance_km / 3.0))

    # Rating score: 0-5 star scale → 0-30 points
    rating_score = (rating / 5.0) * 30.0

    # Trust score: logarithmic scale of review count → 0-20 points
    # 0 reviews = 0, 10 reviews = 10, 100 reviews = 20
    trust_score = min(20.0, 10.0 * math.log10(reviews_count + 1))

    # Availability bonus: 0-10 points
    availability_score = 0.0
    if is_online:
        availability_score += 6.0
    if is_verified:
        availability_score += 4.0

    total = proximity_score + rating_score + trust_score + availability_score
    return round(min(100.0, total), 1)


# ═══════════════════════════════════════════════════════
#  AUTH ROUTES
# ═══════════════════════════════════════════════════════

@app.route("/auth/register", methods=["POST"])
def register():
    """Register a new user with email, password, location, and role-specific fields."""
    data = request.get_json()
    name = data.get("name", "").strip()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    role = data.get("role", "seeker")
    location_text = data.get("location", "").strip()

    # Provider-specific fields
    specialization = data.get("specialization", "")
    price_per_hour = data.get("price_per_hour", 0)

    # Validation
    if not name or not email or not password:
        return jsonify({"error": "Name, email, and password are required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if role not in ("seeker", "provider"):
        return jsonify({"error": "Role must be 'seeker' or 'provider'"}), 400
    if not location_text:
        return jsonify({"error": "Location is required"}), 400

    # Check duplicate email
    if users_collection.find_one({"email": email}):
        return jsonify({"error": "An account with this email already exists"}), 409

    # Geocode the address
    coords = geocode_address(location_text)
    if not coords:
        return jsonify({"error": "Could not find that location. Please enter a valid city or address."}), 400

    lat, lng = coords

    # Build user document
    new_user = {
        "name": name,
        "email": email,
        "password": hash_password(password),
        "role": role,
        "location_text": location_text,
        "location": {
            "type": "Point",
            "coordinates": [lng, lat]  # GeoJSON: [longitude, latitude]
        },
        "is_verified": False,
        "is_online": False,
        "created_at": datetime.datetime.utcnow(),
    }

    # Add provider-specific fields
    if role == "provider":
        new_user["specialization"] = specialization
        new_user["price_per_hour"] = float(price_per_hour) if price_per_hour else 30.0
        new_user["rating"] = 0.0
        new_user["reviews_count"] = 0

    result = users_collection.insert_one(new_user)
    new_user["_id"] = result.inserted_id

    token = generate_token(result.inserted_id)
    return jsonify({
        "token": token,
        "user": serialize_user(new_user),
    }), 201


@app.route("/auth/login", methods=["POST"])
def login():
    """Login with email & password."""
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    user = users_collection.find_one({"email": email})
    if not user:
        return jsonify({"error": "Invalid email or password"}), 401

    if not check_password(password, user["password"]):
        return jsonify({"error": "Invalid email or password"}), 401

    token = generate_token(user["_id"])
    return jsonify({
        "token": token,
        "user": serialize_user(user),
    }), 200


@app.route("/auth/me", methods=["GET"])
@token_required
def get_me(current_user):
    """Returns the currently authenticated user's data."""
    return jsonify({"user": serialize_user(current_user)}), 200


# ═══════════════════════════════════════════════════════
#  PROFESSIONAL ROUTES
# ═══════════════════════════════════════════════════════

@app.route("/api/professionals/nearby", methods=["GET"])
@token_required
def get_nearby_professionals(current_user):
    """
    AI-Powered Recommendation Engine:
    Find professionals near the customer and rank them
    using our scoring algorithm.
    
    Query params:
      - category: filter by specialization (optional)
      - search: search by name or title (optional)
      - radius: search radius in km (default: 50)
    """
    # Get customer's location
    user_location = current_user.get("location")
    if not user_location or not user_location.get("coordinates"):
        return jsonify({"error": "Your location is not set"}), 400

    category = request.args.get("category", "").strip()
    search = request.args.get("search", "").strip()
    radius_km = float(request.args.get("radius", 50))

    # Convert km to meters for MongoDB
    radius_meters = radius_km * 1000

    # Build match filter
    match_filter = {"role": "provider"}
    if category and category != "all":
        match_filter["specialization"] = category
    if search:
        match_filter["$or"] = [
            {"name": {"$regex": search, "$options": "i"}},
            {"specialization": {"$regex": search, "$options": "i"}},
        ]

    # MongoDB $geoNear aggregation — finds professionals sorted by distance
    pipeline = [
        {
            "$geoNear": {
                "near": {
                    "type": "Point",
                    "coordinates": user_location["coordinates"]  # [lng, lat]
                },
                "distanceField": "distance_meters",
                "maxDistance": radius_meters,
                "spherical": True,
                "query": match_filter
            }
        },
        {"$limit": 20}
    ]

    professionals = list(users_collection.aggregate(pipeline))

    # Apply AI scoring to each professional
    results = []
    for pro in professionals:
        distance_km = pro.get("distance_meters", 0) / 1000.0
        rating = pro.get("rating", 0)
        reviews = pro.get("reviews_count", 0)
        is_verified = pro.get("is_verified", False)
        is_online = pro.get("is_online", False)

        ai_score = calculate_ai_score(distance_km, rating, reviews, is_verified, is_online)

        results.append({
            "id": str(pro["_id"]),
            "name": pro.get("name", ""),
            "email": pro.get("email", ""),
            "specialization": pro.get("specialization", ""),
            "rating": rating,
            "reviews_count": reviews,
            "price_per_hour": pro.get("price_per_hour", 0),
            "distance_km": round(distance_km, 1),
            "is_verified": is_verified,
            "is_online": is_online,
            "location_text": pro.get("location_text", ""),
            "ai_score": ai_score,
        })

    # Sort by AI score (highest first)
    results.sort(key=lambda x: x["ai_score"], reverse=True)

    return jsonify({
        "professionals": results,
        "total": len(results),
        "radius_km": radius_km,
    }), 200


@app.route("/api/professionals/status", methods=["PUT"])
@token_required
def update_status(current_user):
    """Toggle professional online/offline status."""
    if current_user.get("role") != "provider":
        return jsonify({"error": "Only professionals can update status"}), 403

    data = request.get_json()
    is_online = data.get("is_online", False)

    users_collection.update_one(
        {"_id": current_user["_id"]},
        {"$set": {"is_online": bool(is_online)}}
    )

    return jsonify({"is_online": bool(is_online)}), 200


# ── Health Check ────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "HOMIE API"}), 200


# ═══════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 50)
    print("  🏠 HOMIE Backend API")
    print(f"  MongoDB: {MONGO_URI}")
    print("  Routes:")
    print("    POST /auth/register")
    print("    POST /auth/login")
    print("    GET  /auth/me")
    print("    GET  /api/professionals/nearby")
    print("    PUT  /api/professionals/status")
    print("=" * 50)
    app.run(debug=True, port=5000)
