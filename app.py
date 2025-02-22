from flask import Flask, request, jsonify, Response, send_from_directory
import google.generativeai as genai
import os
from dotenv import load_dotenv
import boto3
import io
from PIL import Image
import json  # Import the json module
import re  # Import the regular expression module

load_dotenv()  # Load environment variables from .env file

app = Flask(__name__)

# Serve the index.html file from the static directory
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

# Securely retrieve API key from environment variable
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY environment variable not set.")

genai.configure(api_key=GOOGLE_API_KEY)

# Initialize the generative model
model = genai.GenerativeModel('gemini-2.0-flash-exp') # Changed model

# Configure AWS S3
S3_BUCKET = os.environ.get("S3_BUCKET")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY")

s3 = boto3.client(
    "s3",
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
)

def try_parse_json(text):
    """Attempts to parse JSON from a text string, handling potential errors."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"JSONDecodeError: {e}")
        print(f"Attempting to fix JSON: {text}")
        # Attempt to fix common JSON errors
        text = re.sub(r"\\'", "'", text)  # Fix escaped single quotes
        text = re.sub(r"\\\n", "", text)   # Remove escaped newlines

        try:
            return json.loads(text)  # Try parsing again after fixing
        except:
            print("Failed to fix, returning empty dictionary")
            return {}
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return {}


@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()
    prompt = data.get('prompt')
    if not prompt:
        return jsonify({'error': 'Prompt is required'}), 400

    try:
        response = model.generate_content(prompt)
        return jsonify({'response': response.text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    try:
        # Save file to S3
        s3.upload_fileobj(file, S3_BUCKET, file.filename)
        file_url = f"https://{S3_BUCKET}.s3.amazonaws.com/{file.filename}" # Construct S3 URL

        # Read the image from S3
        image_object = s3.get_object(Bucket=S3_BUCKET, Key=file.filename)
        image_data = image_object['Body'].read()

        # Open the image using PIL (from Pillow)
        image = Image.open(io.BytesIO(image_data))

        # Prepare the prompt for the Gemini Pro Vision model
        prompt = """You are an expert in extracting structured data from images of documents. Your task is to extract the following information from the provided image and return it as a JSON object. Ensure the output is valid JSON, even if some values are unknown.

If a particular field cannot be reliably extracted from the image, set its value to null. DO NOT include any preamble, explanation, or other text outside of the JSON structure. The output MUST be a single, valid JSON object.

Here are the fields to extract:

* `country`: The country that issued the document (string, e.g., "UNITED ARAB EMIRATES").
* `residence_type`: The type of residence permit (string, e.g., "RESIDENCE").
* `residence_status`: The residence status (string, if available, e.g., "إقامة جديدة").
* `id_number`: The ID number on the document (string).
* `file_number`: The file number on the document (string).
* `passport_number`: The passport number on the document (string).
* `name`: The full name of the person on the document (string).
* `arabic_name`: The name in Arabic (string, if present).
* `occupation`: The person's occupation (string).
* `company`: The name of the company (string, if applicable).
* `issue_date`: The date the document was issued (string, format "YYYY/MM/DD").
* `expiry_date`: The document's expiry date (string, format "YYYY/MM/DD").
* `place_of_issue`: The place where the document was issued (string, e.g., "DUBAI").

Ensure that every field listed above is present in the output JSON, even if the value is null.
"""

        # Make the Gemini Pro Vision API call
        response = model.generate_content([prompt, image])
        json_output = response.text

        # Attempt to parse the JSON using the helper function
        parsed_json = try_parse_json(json_output)

        return jsonify({'message': 'File uploaded and analyzed successfully', 'url': file_url, 'json_data': parsed_json}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Real-time streaming (example using Server-Sent Events)
@app.route('/stream')
def stream():
    def generate():
        prompt = request.args.get('prompt', 'Tell me a story')
        for chunk in model.generate_content(prompt, stream=True):
            yield f"data: {chunk.text}\n\n"

    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000) # Make sure this port is open in the Security Group