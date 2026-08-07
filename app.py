from flask import Flask, render_template, request, send_file, flash, redirect, url_for
import pandas as pd
import requests
import re
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

app = Flask(__name__)
app.secret_key = "change-this-to-anything-random"

# ====================== PUT YOUR API KEY HERE ======================
API_KEY = "ctp_live_kmENri6SA29x9fTpPODbiuK1eqZeD8W0"
# ===================================================================

def clean_phone(phone):
    if pd.isna(phone):
        return None
    phone = re.sub(r"[^\d]", "", str(phone))
    # Extract the standard 10-digit subscriber portion so the API maps properly.
    if len(phone) == 11 and phone.startswith("1"):
        return phone[1:]
    if len(phone) == 10:
        return phone
    return None

def lookup_number(phone):
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    try:
        # Pass litigatorFilter in the URL query string as required by the API specification
        url = "https://checkthatphone.com"
        
        r = requests.post(
            url,
            headers=headers,
            json={"number": phone},
            timeout=30
        )
        
        if r.status_code == 200:
            res_json = r.json()
            # If the API backend itself returned a success: false token, log it
            if not res_json.get("success"):
                print(f"API returned success=False for {phone}: {res_json}")
            return res_json
            
        print(f"HTTP Error {r.status_code} for {phone}: {r.text}")
        return {"success": False}
    except Exception as e:
        print(f"Connection Exception for {phone}: {str(e)}")
        return {"success": False}

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
                
            results = {}
            with ThreadPoolExecutor(max_workers=5) as executor:
                # Running lookup against unique numbers protects your credits from list duplication
                futures = {executor.submit(lookup_number, p): p for p in df["clean_phone"].unique()}
                for future in as_completed(futures):
                    phone = futures[future]
                    results[phone] = future.result()
                    time.sleep(0.05)
                    
            df["deliverable"] = df["clean_phone"].map(
                lambda x: str(results.get(x, {}).get("data", {}).get("deliverable", "false")).lower()
            )
            df["action"] = df["clean_phone"].map(
                lambda x: results.get(x, {}).get("data", {}).get("action", "")
            )
            df["carrier"] = df["clean_phone"].map(
                lambda x: results.get(x, {}).get("data", {}).get("dipCarrier", "")
            )
            df["line_type"] = df["clean_phone"].map(
                lambda x: results.get(x, {}).get("data", {}).get("dipCarrierType", "")
            )
            df["reason"] = df["clean_phone"].map(
                lambda x: results.get(x, {}).get("data", {}).get("reason", "")
            )
            
            # Filter logic: Keep rows that are explicitly deliverable and do not have an unsubscribe recommendation
            cleaned = df[(df["deliverable"] == "true") & (df["action"] != "unsubscribe")].copy()
            cleaned = cleaned.drop(columns=["clean_phone"])
            
            # Save to temp file for download
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
