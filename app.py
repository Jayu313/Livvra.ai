# ================= IMPORTS =================
from flask import (
    Flask, render_template, request, redirect,
    session, url_for, jsonify, send_file
)
import mysql.connector
from ml_model import forecast_price
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from flask_mail import Mail, Message
import csv, os
from datetime import datetime
from authlib.integrations.flask_client import OAuth

# ================= PASSWORD RESET RATE LIMIT =================
RESET_LIMIT = 3          # max attempts
RESET_WINDOW_MIN = 15    # minutes

# ================= APP CONFIG =================
app = Flask(__name__)
app.secret_key = "livvra-secret"

# ================= OAUTH CONFIG =================
oauth = OAuth(app)

google = oauth.register(
    name="google",
    client_id="your_google_client_id",
    client_secret="your_google_client_secret",
    access_token_url="https://accounts.google.com/o/oauth2/token",
    access_token_params=None,
    authorize_url="https://accounts.google.com/o/oauth2/auth",
    authorize_params=None,
    api_base_url="https://www.googleapis.com/oauth2/v1/",
    userinfo_endpoint="https://www.googleapis.com/oauth2/v1/userinfo",
    client_kwargs={"scope": "openid email profile"},
)

# ================= EMAIL CONFIG =================
app.config.update(
    MAIL_SERVER="smtp.gmail.com",
    MAIL_PORT=587,
    MAIL_USE_TLS=True,
    MAIL_USERNAME="yourgmail@gmail.com",
    MAIL_PASSWORD="your_app_password"
)
mail = Mail(app)

# ================= FILE UPLOAD CONFIG =================
UPLOAD_FOLDER = "static/uploads"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# ================= DATABASE =================
db = mysql.connector.connect(
    host="localhost",
    user="root",
    password="",
    database="livvra"
)
cursor = db.cursor(dictionary=True)

# ================= RATE LIMIT CHECK =================
def reset_rate_limited(email, ip):
    cursor.execute("""
        SELECT COUNT(*) AS cnt
        FROM password_reset_requests
        WHERE (email=%s OR ip_address=%s)
          AND created_at > (NOW() - INTERVAL %s MINUTE)
    """, (email, ip, RESET_WINDOW_MIN))

    result = cursor.fetchone()
    return result["cnt"] >= RESET_LIMIT

# ================= PATHS =================
BASE_DIR = os.getcwd()
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

# ================= LOGIN =================
@app.route("/", methods=["GET", "POST"])
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
        user = cursor.fetchone()

        if user and check_password_hash(user["password"], password):
            session["user"] = user
            return redirect("/dashboard")

        return render_template("login.html", error="Invalid credentials", hide_navbar=True)

    return render_template("login.html", hide_navbar=True)

# ================= GOOGLE OAUTH =================
@app.route("/login/google")
def google_login():
    redirect_url = url_for("google_callback", _external=True)
    return google.authorize_redirect(redirect_url)

@app.route("/login/google/callback")
def google_callback():
    token = google.authorize_access_token()
    resp = google.get("userinfo")
    user_info = resp.json()

    email = user_info["email"]
    name = user_info["name"]

    cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
    user = cursor.fetchone()

    if not user:
        cursor.execute(
            "INSERT INTO users (name, email, password, role) VALUES (%s, %s, %s, 'user')",
            (name, email, generate_password_hash(os.urandom(16).hex()))
        )
        db.commit()
        cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
        user = cursor.fetchone()

    session["user"] = user
    return redirect("/dashboard")

# ================= Apple OAUTH =================
@app.route("/login/apple")
def apple_login():
    # Placeholder for Apple OAuth implementation
    return "Apple OAuth not implemented yet", 501

# ================= REGISTER =================
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name")
        email = request.form.get("email")
        password = generate_password_hash(request.form.get("password"))

        cursor.execute("SELECT id FROM users WHERE email=%s", (email,))
        if cursor.fetchone():
            return render_template("register.html", error="Email already exists")

        cursor.execute(
            "INSERT INTO users (name,email,password,role) VALUES (%s,%s,%s,'user')",
            (name, email, password)
        )
        db.commit()
        return redirect("/login")

    return render_template("register.html", hide_navbar=True)

# ================= LOGOUT =================
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ================= DASHBOARD =================
@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/login", hide_navbar=True)

    uid = session["user"]["id"]

    cursor.execute("""
        SELECT
            COUNT(*) AS total,
            COALESCE(AVG(predicted_price - base_price), 0) AS avg_profit,
            SUM(predicted_price > base_price) AS buy,
            SUM(predicted_price = base_price) AS hold,
            SUM(predicted_price < base_price) AS sell
        FROM predictions
        WHERE user_id=%s AND is_deleted=0
    """, (uid,))

    stats = cursor.fetchone()

    return render_template(
        "dashboard.html",
        user=session["user"],
        stats=stats
    )

# ================= HOME =================
@app.route("/home")
def home():
    if "user" not in session:
        return redirect("/login", hide_navbar=True)
    return render_template("home.html", current_year=datetime.now().year)

# ================= LISTINGS =================
@app.route("/listings")
def listings():
    intent = request.args.get("intent")
    q = request.args.get("q")
    min_price = request.args.get("min_price")
    max_price = request.args.get("max_price")

    sql = "SELECT * FROM property_listing WHERE status='ACTIVE'"
    params = []

    if intent:
        sql += " AND intent=%s"
        params.append(intent)

    if q:
        sql += " AND (title LIKE %s OR description LIKE %s)"
        params.extend([f"%{q}%", f"%{q}%"])

    if min_price:
        sql += " AND price >= %s"
        params.append(min_price)

    if max_price:
        sql += " AND price <= %s"
        params.append(max_price)

    sql += " ORDER BY created_at DESC"

    cursor.execute(sql, tuple(params))
    listings = cursor.fetchall()

    return render_template("listing.html", listings=listings)

# ================= FAVORITE PROPERTIES (NEW) =================
@app.route("/favorites")
def favorites():
    if "user" not in session:
        return redirect("/login")

    uid = session["user"]["id"]

    cursor.execute("""
        SELECT p.*
        FROM property_listing p
        JOIN favorites f ON f.property_id = p.id
        WHERE f.user_id=%s
        ORDER BY f.created_at DESC
    """, (uid,))

    listings = cursor.fetchall()
    return render_template("listing.html", listings=listings)


# ================= TOGGLE FAVORITE =================
@app.route("/favorite/<int:pid>", methods=["POST"])
def toggle_favorite(pid):
    if "user" not in session:
        return jsonify({"error": "login"}), 401

    uid = session["user"]["id"]

    cursor.execute("""
        SELECT id FROM favorites
        WHERE user_id=%s AND property_id=%s
    """, (uid, pid))

    if cursor.fetchone():
        cursor.execute("""
            DELETE FROM favorites
            WHERE user_id=%s AND property_id=%s
        """, (uid, pid))
        db.commit()
        return jsonify({"status": "removed"})
    else:
        cursor.execute("""
            INSERT INTO favorites (user_id, property_id, created_at)
            VALUES (%s,%s,NOW())
        """, (uid, pid))
        db.commit()
        return jsonify({"status": "saved"})

# ================= CHECK FAVORITE =================
@app.route("/favorite/check/<int:pid>")
def check_favorite(pid):
    if "user" not in session:
        return jsonify({"favorite": False})

    uid = session["user"]["id"]

    cursor.execute("""
        SELECT id FROM favorites
        WHERE user_id=%s AND property_id=%s
    """, (uid, pid))

    if cursor.fetchone():
        return jsonify({"favorite": True})
    else:
        return jsonify({"favorite": False})

# ================= MY LISTINGS =================
@app.route("/my-listings")
def my_listings():
    if "user" not in session:
        return redirect("/login")

    cursor.execute("""
        SELECT * FROM property_listing
        WHERE user_id=%s
        ORDER BY created_at DESC
    """, (session["user"]["id"],))

    listings = cursor.fetchall()
    return render_template("my_listings.html", listings=listings)

# ================= PROPERTY DETAILS =================
@app.route("/property/<int:pid>")
def property_details(pid):
    cursor.execute("""
        SELECT p.*, u.name, u.email
        FROM property_listing p
        JOIN users u ON u.id = p.user_id
        WHERE p.id=%s
    """, (pid,))
    prop = cursor.fetchone()

    if not prop:
        return "Property not found", 404

    return render_template("property_details.html", prop=prop)

# ================= ADD LISTING =================
@app.route("/listings/add", methods=["GET", "POST"])
def add_listing():
    if "user" not in session:
        return redirect("/login")

    if request.method == "POST":
        title = request.form.get("title")
        description = request.form.get("description")
        price = request.form.get("price")
        intent = request.form.get("intent")

        image = request.files.get("image")
        image_filename = None

        if image and allowed_file(image.filename):
            filename = secure_filename(image.filename)
            image_filename = f"{int(datetime.now().timestamp())}_{filename}"
            image.save(os.path.join(app.config["UPLOAD_FOLDER"], image_filename))

        cursor.execute("""
            INSERT INTO property_listing
            (user_id,title,description,price,intent,image,status,created_at)
            VALUES (%s,%s,%s,%s,%s,%s,'ACTIVE',NOW())
        """, (
            session["user"]["id"],
            title, description, price, intent, image_filename
        ))
        db.commit()

        return redirect("/listings")

    return render_template("add_listing.html")

# ================= EDIT LISTING =================
@app.route("/listings/edit/<int:pid>", methods=["GET", "POST"])
def edit_listing(pid):
    if "user" not in session:
        return redirect("/login")

    cursor.execute("""
        SELECT * FROM property_listing
        WHERE id=%s AND user_id=%s
    """, (pid, session["user"]["id"]))

    listing = cursor.fetchone()

    if not listing:
        return "Listing not found or unauthorized", 404

    if request.method == "POST":
        title = request.form.get("title")
        description = request.form.get("description")
        price = request.form.get("price")
        intent = request.form.get("intent")

        image = request.files.get("image")
        image_filename = listing["image"]

        if image and allowed_file(image.filename):
            filename = secure_filename(image.filename)
            image_filename = f"{int(datetime.now().timestamp())}_{filename}"
            image.save(os.path.join(app.config["UPLOAD_FOLDER"], image_filename))

        cursor.execute("""
            UPDATE property_listing
            SET title=%s, description=%s, price=%s, intent=%s, image=%s
            WHERE id=%s AND user_id=%s
        """, (
            title, description, price, intent,
            image_filename, pid, session["user"]["id"]
        ))
        db.commit()

        return redirect("/listings")

    return render_template("edit_listing.html", listing=listing)

# ================= DELETE LISTING =================
@app.route("/listings/delete/<int:pid>", methods=["POST"])
def delete_listing(pid):
    if "user" not in session:
        return redirect("/login")

    cursor.execute("""
        DELETE FROM property_listing
        WHERE id=%s AND user_id=%s
    """, (pid, session["user"]["id"]))
    db.commit()

    return redirect("/listings")

# ================= HISTORY =================
@app.route("/history")
def history():
    if "user" not in session:
        return redirect("/login")

    cursor.execute("""
        SELECT *
        FROM predictions
        WHERE user_id=%s AND is_deleted=0
        ORDER BY created_at DESC
    """, (session["user"]["id"],))

    history = cursor.fetchall()
    return render_template("history.html", history=history)

# ================= BULK DELETE HISTORY =================
@app.route("/history/delete-bulk", methods=["POST"])
def delete_bulk_history():
    if "user" not in session:
        return redirect("/login")

    ids = request.form.getlist("prediction_ids")

    if ids:
        placeholders = ",".join(["%s"] * len(ids))
        cursor.execute(
            f"""
            UPDATE predictions
            SET is_deleted=1, deleted_at=NOW()
            WHERE id IN ({placeholders}) AND user_id=%s
            """,
            (*ids, session["user"]["id"])
        )
        db.commit()

    session["undo_ids"] = ids
    session["toast"] = "Predictions deleted"
    return redirect("/history")

# ================= UNDO DELETE HISTORY =================
@app.route("/history/undo")
def undo_delete():
    if "user" not in session:
        return redirect("/login")

    ids = session.get("undo_ids")

    if ids:
        placeholders = ",".join(["%s"] * len(ids))
        cursor.execute(
            f"""
            UPDATE predictions
            SET is_deleted=0, deleted_at=NULL
            WHERE id IN ({placeholders}) AND user_id=%s
            """,
            (*ids, session["user"]["id"])
        )
        db.commit()

    session.pop("undo_ids", None)
    session["toast"] = "Undo successful"
    return redirect("/history")

# ================= EXPORT HISTORY =================
@app.route("/history/export")
def export_history():
    if "user" not in session:
        return redirect("/login")

    cursor.execute("""
        SELECT created_at, area_sqft, base_price, years, predicted_price
        FROM predictions
        WHERE user_id=%s
        ORDER BY created_at DESC
    """, (session["user"]["id"],))

    rows = cursor.fetchall()
    filename = f"prediction_history_{session['user']['id']}.csv"

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Date", "Area (sqft)", "Base Price", "Years", "Predicted Price"
        ])
        for r in rows:
            writer.writerow([
                r["created_at"], r["area_sqft"],
                r["base_price"], r["years"], r["predicted_price"]
            ])

    return send_file(filename, as_attachment=True)

# ================= REPORT GENERATION =================
@app.route("/history/report/<int:pid>")
def generate_report(pid):
    if "user" not in session:
        return redirect("/login")

    cursor.execute("""
        SELECT *
        FROM predictions
        WHERE id=%s AND user_id=%s
    """, (pid, session["user"]["id"]))

    prediction = cursor.fetchone()

    if not prediction:
        return "Prediction not found", 404

    report_filename = os.path.join(
        REPORTS_DIR,
        f"report_{pid}_{session['user']['id']}.pdf"
    )

    c = canvas.Canvas(report_filename, pagesize=A4)
    width, height = A4

    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, height - 50, "Property Price Prediction Report")

    c.setFont("Helvetica", 12)
    c.drawString(50, height - 100, f"User: {session['user']['name']}")
    c.drawString(50, height - 120, f"Email: {session['user']['email']}")
    c.drawString(50, height - 140, f"Area (sqft): {prediction['area_sqft']}")
    c.drawString(50, height - 160, f"Base Price: ${prediction['base_price']}")
    c.drawString(50, height - 180, f"Years Forecasted: {prediction['years']}")
    c.drawString(50, height - 200, f"Predicted Price: ${prediction['predicted_price']}")

    c.showPage()
    c.save()

    return send_file(report_filename, as_attachment=True)

# ================= PROFILE =================
@app.route("/profile", methods=["GET", "POST"])
def profile():
    if "user" not in session:
        return redirect("/login")
    
    if request.method == "POST":
        name = request.form.get("name")
        email = request.form.get("email")

        cursor.execute("""
            UPDATE users
            SET name=%s, email=%s
            WHERE id=%s
        """, (name, email, session["user"]["id"]))
        db.commit()

        session["user"]["name"] = name
        session["user"]["email"] = email

        return redirect("/profile")
    return render_template("profile.html", user=session["user"])

# ================= FORGOT PASSWORD =================
@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email")
        ip = request.remote_addr

        # Rate limit check
        if reset_rate_limited(email, ip):
            return render_template(
                "forgot_password.html",
                error="Too many requests. Please try again after 15 minutes."
            )

        # Store attempt
        cursor.execute("""
            INSERT INTO password_reset_requests (email, ip_address)
            VALUES (%s,%s)
        """, (email, ip))
        db.commit()

        # Check user exists
        cursor.execute("SELECT id FROM users WHERE email=%s", (email,))
        user = cursor.fetchone()

        # IMPORTANT: same response whether user exists or not
        # (prevents email enumeration)
        if user:
            # TODO (next step): generate token + send email
            pass

        return render_template(
            "forgot_password.html",
            success="Reset link sent if email exists."
        )

    return render_template("forgot_password.html")


# ================= API: PREDICT =================
@app.route("/api/predict", methods=["POST"])
def api_predict():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    d = request.json

    area = float(d["area"])
    base_price = float(d["price"])
    years_selected = int(d["years"])
    age = int(d.get("age", 5))
    scenario = d.get("scenario", "neutral")
    lat = float(d.get("lat", 0))
    lng = float(d.get("lng", 0))

    years = list(range(1, years_selected + 1))
    prices = []

    for y in years:
        meta = forecast_price(area, y, age, base_price, scenario)
        prices.append(meta["price"])

    final_price = prices[-1]
    profit_loss = round(final_price - base_price, 2)
    price_change = round((profit_loss / base_price) * 100, 2)

    if prices[0] < base_price and final_price < base_price:
        recommendation = "SELL"
        explanation = "Property value declines and does not recover."
    elif prices[0] < base_price and final_price > base_price:
        recommendation = "HOLD"
        explanation = "Initial dip followed by recovery."
    else:
        recommendation = "BUY"
        explanation = "Steady appreciation observed."

    best_year_index = prices.index(max(prices))
    best_year = best_year_index + 1
    best_year_price = prices[best_year_index]
    risk = "Low" if abs(price_change) < 3 else "Medium" if abs(price_change) < 8 else "High"

    cursor.execute("""
        INSERT INTO predictions
        (user_id, area_sqft, base_price, years, predicted_price, lat, lng, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
    """, (
        session["user"]["id"],
        area, base_price, years_selected,
        final_price, lat, lng
    ))
    db.commit()

    return jsonify({
        "years": years,
        "prices": prices,
        "final_price": final_price,
        "price_change": price_change,
        "profit_loss": profit_loss,
        "best_year": best_year,
        "best_year_price": best_year_price,
        "risk": risk,
        "recommendation": recommendation,
        "confidence": meta["confidence"],
        "area_stage": meta["area_stage"],
        "explanation": explanation
    })

# ================= RUN =================
if __name__ == "__main__":
    app.run(debug=True)
