from flask import Flask, request, jsonify, send_from_directory
import google.generativeai as genai
import os
from dotenv import load_dotenv
import boto3
import io
from PIL import Image
import json  # Import the json module
import re  # Import the regular expression module
from pymongo import MongoClient
from bson.objectid import ObjectId #Import Object ID
load_dotenv()  # Load environment variables from .env file

app = Flask(__name__)

# Custom JSON Encoder to handle ObjectId
class JSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, ObjectId):
            return str(o)  # Convert ObjectId to its string representation
        return json.JSONEncoder.default(self, o)

app.json_encoder = JSONEncoder

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

# MongoDB Configuration
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")  # Default to localhost
DATABASE_NAME = os.environ.get("DATABASE_NAME", "ai_data")  # Default database name
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "image_data")  # Default collection name

try:
    client = MongoClient(MONGO_URI)
    db = client[DATABASE_NAME]
    collection = db[COLLECTION_NAME]
    # Try to perform a simple operation to test the connection
    client.admin.command('ping')
    print("Connected to MongoDB successfully!")
except Exception as e:
    print(f"Error connecting to MongoDB: {e}")
    # You might want to handle this more gracefully in a production environment
    collection = None  # Disable MongoDB functionality if the connection fails


def try_parse_json(text):
    """Attempts to parse JSON from a text string, handling potential errors."""
    try:
        # Remove any characters before the first '{' and after the last '}'
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1:
            json_text = text[start:end + 1]
            #Aggressively remove escapes
            json_text = str(json_text).replace('""', '"').replace("\\\\", "\\").replace("\n", "").replace("'", "\"")
            data =  json.loads(json_text)
            # Apply string conversion to all values in data
            for key, value in data.items():
                if value is not None:
                    data[key] = str(value)
            return data
        else:
            print("No valid JSON start or end delimiters found")
            return {}  # Return an empty dictionary if no JSON is found
    except json.JSONDecodeError as e:
        print(f"JSONDecodeError: {e}")
        print(f"Attempting to fix JSON: {text}")
        # Attempt to fix common JSON errors
        text = re.sub(r"\\'", "'", text)  # Fix escaped single quotes
        text = re.sub(r"\\\n", "", text)   # Remove escaped newlines

        try:
            return json.loads(text)  # Try parsing again after fixing
        except Exception as ex: #Add Exception
            print("Failed to fix, returning empty dictionary")
            return {}
    except Exception as e: #Add exception
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
        prompt = """You are an expert in extracting structured data from images of documents.
Your task is to extract the following information from the provided image and return it as a JSON object.
If a particular field cannot be reliably extracted from the image, set its value to null.
DO NOT include any preamble, explanation, or other text outside of the JSON structure.
The output MUST be a single, valid JSON object.

Here are the fields to extract:
* country (string, e.g., "UNITED ARAB EMIRATES"): The country that issued the document.
* residence_type (string, e.g., "RESIDENCE"): The type of residence permit.
* residence_status (string, if available, e.g., "إقامة جديدة").
* id_number (string): The ID number on the document.
* file_number (string): The file number on the document.
* passport_number (string): The passport number on the document.
* name (string): The full name of the person on the document.
* arabic_name (string, if present): The name in Arabic.
* occupation (string): The person's occupation.
* company (string, if applicable): The name of the company.
* issue_date (string, format "YYYY/MM/DD"): The date the document was issued.
* expiry_date (string, format "YYYY/MM/DD"): The document's expiry date.
* place_of_issue (string, e.g., "DUBAI"): The place where the document was issued.

Ensure that every field listed above is present in the output JSON, even if the value is null.
"""

        # Make the Gemini Pro Vision API call
        response = model.generate_content([prompt, image])

         # Check for safety violations
        if response.prompt_feedback and response.prompt_feedback.block:
            print("Prompt was blocked due to safety concerns.")
            return jsonify({'message': 'File uploaded, but analysis blocked due to safety concern', 'url': file_url, 'json_data': {}}), 200

        json_output = response.text
        print(f"Raw JSON Output from Gemini: {json_output}")  # Add this line

        # Attempt to parse the JSON using the helper function
        parsed_json = try_parse_json(json_output)
        print(f"Parsed JSON: {parsed_json}")

        # Store data in MongoDB (if the connection is successful and the data is parsed)
        if collection is not None and parsed_json:
            try:
                # Add extra data before inserting
                parsed_json['file_url'] = file_url
                insert_result = collection.insert_one(parsed_json)
                print(f"Data inserted into MongoDB with _id: {insert_result.inserted_id}")
            except Exception as e:
                print(f"Error inserting data into MongoDB: {e}")
                return jsonify({'message': 'File uploaded, analyzed, but MongoDB insertion failed', 'url': file_url, 'json_data': {}}), 200

        return jsonify({'message': 'File uploaded and analyzed successfully', 'url': file_url, 'json_data': parsed_json}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000) # Make sure this port is open in the Security Group