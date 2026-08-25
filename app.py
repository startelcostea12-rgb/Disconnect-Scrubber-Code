from flask import Flask, render_template, render_template_string, request, send_file, flash, redirect, url_for
import pandas as pd
import requests
import re
import os
import sys
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

app = Flask(__name__)
app.secret_key = "change-this-to-anything-random"

# ====================== API KEY ======================
API_KEY = "ctp_live_szbun8EEXauNTe6AIJPVZ3JagVRAXe1V"
# =======================================================

# In-memory job tracker. Fine for a single-instance app (WEB_CONCURRENCY=1).
# If you ever scale to multiple instances, this needs to move to a shared store (e.g. Redis).
JOBS = {}
JOBS_LOCK = threading.Lock()

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
        url = "https://api.checkthatphone.com/v1/lookup"
        payload = {
            "phone": phone,
            "litigatorFilter": False  # flip to True if you want litigator scrubbing back on
        }
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code == 200:
            return r.json()
        print(f"RENDER LOG - HTTP ERROR {r.status_code} for {phone}: {r.text}")
        sys.stdout.flush()
        return {"success": False, "error_detail": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        print(f"RENDER LOG - EXCEPTION for {phone}: {str(e)}")
        sys.stdout.flush()
        return {"success": False, "error_detail": f"Request failed: {e}"}

def process_job(job_id, filepath):
    try:
        df = pd.read_csv(filepath, dtype=str)

        if "phone" not in df.columns:
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["error"] = "CSV must have a column named 'phone'"
            return

        df["clean_phone"] = df["phone"].apply(clean_phone)
        df = df[df["clean_phone"].notna()].copy()
        total = len(df)

        with JOBS_LOCK:
            JOBS[job_id]["total"] = total

        if total == 0:
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["error"] = "No valid phone numbers found"
            return

        unique_numbers = df["clean_phone"].unique().tolist()
        results = {}
        processed_count = 0

        # 20 concurrent workers -- tune this down if you start seeing rate-limit errors (HTTP 429)
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(lookup_number, p): p for p in unique_numbers}
            for future in as_completed(futures):
                phone = futures[future]
                results[phone] = future.result()
                processed_count += 1
                if processed_count % 50 == 0 or processed_count == len(unique_numbers):
                    with JOBS_LOCK:
                        JOBS[job_id]["processed"] = processed_count
                        JOBS[job_id]["unique_total"] = len(unique_numbers)

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

        cleaned = df[df["deliverable"] == "true"].copy()
        cleaned = cleaned.drop(columns=["clean_phone"])

        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
        cleaned.to_csv(temp.name, index=False)
        temp.close()

        removed = total - len(cleaned)

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["total"] = total
            JOBS[job_id]["kept"] = len(cleaned)
            JOBS[job_id]["removed"] = removed
            JOBS[job_id]["download_name"] = os.path.basename(temp.name)

    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)
    finally:
        try:
            os.remove(filepath)
        except Exception:
            pass

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

        # Save the upload to disk immediately -- the request ends right after this,
        # so we can't keep it in memory for the background thread to use later.
        upload_temp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
        file.save(upload_temp.name)
        upload_temp.close()

        job_id = str(uuid.uuid4())
        with JOBS_LOCK:
            JOBS[job_id] = {"status": "processing", "total": 0, "processed": 0, "unique_total": 0}

        thread = threading.Thread(target=process_job, args=(job_id, upload_temp.name))
        thread.daemon = True
        thread.start()

        return redirect(url_for("status", job_id=job_id))

    return render_template("index.html")

@app.route("/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)

    if not job:
        flash("That job wasn't found -- it may have expired after a server restart. Please upload again.")
        return redirect(url_for("index"))

    if job["status"] == "processing":
        return render_template_string("""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Processing...</title>
            <meta http-equiv="refresh" content="5">
            <style>
                body { font-family: Arial, sans-serif; background: #0f172a; color: white;
                       display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
                .card { background: #1e293b; padding: 40px; border-radius: 16px; width: 420px; text-align: center; }
                .bar-bg { background: #334155; border-radius: 8px; height: 20px; margin-top: 20px; overflow: hidden; }
                .bar-fill { background: #3b82f6; height: 100%; transition: width 0.3s; }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>Processing your list...</h1>
                <p>{{ processed }} / {{ unique_total }} unique numbers checked</p>
                <div class="bar-bg"><div class="bar-fill" style="width: {{ pct }}%;"></div></div>
                <p style="color:#94a3b8; margin-top:20px;">This page refreshes automatically every 5 seconds.</p>
            </div>
        </body>
        </html>
        """, processed=job.get("processed", 0), unique_total=max(job.get("unique_total", 1), 1),
             pct=round((job.get("processed", 0) / max(job.get("unique_total", 1), 1)) * 100))

    if job["status"] == "error":
        flash(f"Error: {job.get('error', 'Unknown error')}")
        return redirect(url_for("index"))

    # done
    return render_template(
        "result.html",
        total=job["total"],
        kept=job["kept"],
        removed=job["removed"],
        download_name=job["download_name"]
    )

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
