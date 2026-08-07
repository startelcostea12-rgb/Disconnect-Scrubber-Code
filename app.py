from flask import Flask, render_template, request, send_file, flash, redirect, url_for
import pandas as pd
import requests
import re
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

app = Flask(__name__)
app.secret_key = "change-this-to-anything-random"

# ====================== API KEY ======================
API_KEY = "ctp_live_kmENri6SA29x9fTpPODbiuK1eqZeD8W0"
# =======================================================

def clean_phone(phone):
    if pd.isna(phone):
        return None
    phone = re.sub(r"[^\d]", "", str(phone))
    if len(phone) == 11 and phone.startswith("1"):
        return phone
    if len(phone) == 10:
        return "1" + phone
    return None

def lookup_number(phone):
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    try:
        # This MUST be the API subdomain + /v1/lookup path -- not the marketing website
        url = "https://api.checkthatphone.com/v1/lookup"
        payload = {
            "phone": phone,
            "litigatorFilter": True
        }

        r = requests.post(url, headers=headers, json=payload, timeout=30)

        if r.status_code == 200:
            res_json = r.json()
            print(f"RENDER LOG - SUCCESS FOR {phone}: {res_json}")
            sys.stdout.flush()
            return res_json

        print(f"RENDER LOG - HTTP ERROR {r.status_code} for {phone}: {r.text}")
        sys.stdout.flush()
        return {"success": False, "error_detail": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        print(f"RENDER LOG - EXCEPTION for {phone}: {str(e)}")
        sys.stdout.flush()
        return {"success": False, "error_detail": f"Request failed: {e}"}

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        if "file" not in request.files:
            flash("No file selected")
            return redirect(url_for("index"))
        file = request.files["file"]
        if file.filename == "":
            flash("No file selected")
            return redirect(url_for("index"))
        if not file.filename.lower().endswith(".csv"):
            flash("Please upload a CSV file")
            return redirect(url_for("index"))

        try:
            df = pd.read_csv(file, dtype=str)
            if "phone" not in df.columns:
                flash("CSV must have a column named 'phone'")
                return redirect(url_for("index"))

            df["clean_phone"] = df["phone"].apply(clean_phone)
            df = df[df["clean_phone"].notna()].copy()
            total = len(df)

            if total == 0:
                flash("No valid phone numbers found")
                return redirect(url_for("index"))

            # Only look up each unique number once -- saves API credits on lists with repeats
            results = {}
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {executor.submit(lookup_number, p): p for p in df["clean_phone"].unique()}
                for future in as_completed(futures):
                    phone = futures[future]
                    results[phone] = future.result()
                    time.sleep(0.05)

            df["deliverable"] = df["clean_phone"].map(
                lambda x: str(results.get(x, {}).get("data", {}).get("deliverable", "false")).lower()
            )
            df["action"] = df["clean_phone"].map(
                lambda x: str(results.get(x, {}).get("data", {}).get("action", "none")).lower()
            )
            df["carrier"] = df["clean_phone"].map(
                lambda x: results.get(x, {}).get("data", {}).get("dipCarrier", "Unknown")
            )
            df["line_type"] = df["clean_phone"].map(
                lambda x: results.get(x, {}).get("data", {}).get("dipCarrierType", "Unknown")
            )
            df["carrier_subtype"] = df["clean_phone"].map(
                lambda x: results.get(x, {}).get("data", {}).get("dipCarrierSubType", "")
            )
            df["ported"] = df["clean_phone"].map(
                lambda x: results.get(x, {}).get("data", {}).get("dipPorted", "")
            )
            df["reason"] = df["clean_phone"].map(
                lambda x: results.get(x, {}).get("data", {}).get("reason", "")
                or results.get(x, {}).get("error_detail", "")
            )

            # Only keep deliverable numbers -- this is what actually removes disconnects/litigators
            cleaned = df[df["deliverable"] == "true"].copy()
            cleaned = cleaned.drop(columns=["clean_phone"])

            temp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
            cleaned.to_csv(temp.name, index=False)
            temp.close()

            removed = total - len(cleaned)

            return render_template(
                "result.html",
                total=total,
                kept=len(cleaned),
                removed=removed,
                download_name=os.path.basename(temp.name)
            )
        except Exception as e:
            flash(f"Error: {str(e)}")
            return redirect(url_for("index"))

    return render_template("index.html")

@app.route("/download/<filename>")
def download(filename):
    filename = os.path.basename(filename)
    filepath = os.path.join(tempfile.gettempdir(), filename)
    if not os.path.exists(filepath):
        flash("That file has expired -- please upload and process again.")
        return redirect(url_for("index"))
    return send_file(filepath, as_attachment=True, download_name="cleaned_leads.csv")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
