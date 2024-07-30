from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient
from bson import ObjectId
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
import datetime
import firebase_admin
from firebase_admin import credentials, messaging
import sendgrid
from sendgrid.helpers.mail import Mail, Email, To, Content
from twilio.rest import Client
from dotenv import load_dotenv
import os

load_dotenv()

app = Flask(__name__)
app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET_KEY')
jwt = JWTManager(app)
CORS(app)

client = MongoClient(os.getenv('MONGO_URI'))
db = client[os.getenv('DB_NAME')]
flights = db.flights
notifications = db.notifications
users = db.users

cred = credentials.Certificate(os.getenv('FIREBASE_CRED_PATH'))
firebase_admin.initialize_app(cred)

SENDGRID_API_KEY = os.getenv('SENDGRID_API_KEY')
SENDGRID_SENDER_EMAIL = os.getenv('SENDGRID_SENDER_EMAIL')
sg = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)

TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER')
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

ADMIN_CREDENTIALS = {
    'username': os.getenv('ADMIN_USERNAME'),
    'password': os.getenv('ADMIN_PASSWORD')
}

@app.route('/')
def home():
    return "Welcome to the Flight Status API!"

@app.route('/admin/login', methods=['POST'])
def admin_login():
    data = request.json
    if data['username'] == ADMIN_CREDENTIALS['username'] and data['password'] == ADMIN_CREDENTIALS['password']:
        access_token = create_access_token(identity={'username': data['username']})
        return jsonify(access_token=access_token)
    return jsonify({'msg': 'Invalid credentials'}), 401

@app.route('/user/register', methods=['POST'])
def user_register():
    data = request.json
    existing_user = users.find_one({'username': data['username']})
    if existing_user:
        return jsonify({'msg': 'Username already exists'}), 409
    users.insert_one({
        'username': data['username'],
        'email': data['email'],
        'phone': data['phone'],
        'password': data['password'],
        'assigned_flights': []
    })
    return jsonify({'msg': 'User registered successfully'}), 201

@app.route('/user/login', methods=['POST'])
def user_login():
    data = request.json
    user = users.find_one({'username': data['username'], 'password': data['password']})
    if user:
        access_token = create_access_token(identity={'username': user['username']})
        return jsonify(access_token=access_token)
    return jsonify({'msg': 'Invalid credentials'}), 401

@app.route('/flights', methods=['POST'])
@jwt_required()
def add_flight():
    flight_data = request.json
    result = flights.insert_one(flight_data)
    return jsonify({'id': str(result.inserted_id)})

@app.route('/flights', methods=['GET'])
@jwt_required()
def get_flights():
    print("GET /flights endpoint hit")  # Debug
    user_identity = get_jwt_identity()
    print(f"User identity: {user_identity}")  # Debug
    user = users.find_one({'username': user_identity['username']})
    if user:
        print(f"User found: {user}")  # Debug
        all_flights = flights.find({'flight_id': {'$in': user['assigned_flights']}})
        return jsonify([{**flight, '_id': str(flight['_id'])} for flight in all_flights])
    print("User not found")  # Debug
    return jsonify({'msg': 'User not found'}), 404

@app.route('/admin/flights', methods=['GET'])
@jwt_required()
def get_all_flights():
    all_flights = flights.find()
    return jsonify([{**flight, '_id': str(flight['_id'])} for flight in all_flights])

@app.route('/flights/<id>', methods=['PUT'])
@jwt_required()
def update_flight(id):
    updates = request.json
    if '_id' in updates:
        del updates['_id']
    result = flights.update_one({'_id': ObjectId(id)}, {'$set': updates})
    if result.modified_count > 0:
        flight = flights.find_one({'_id': ObjectId(id)})
        create_notification(flight)
        return jsonify({'updated': True})
    return jsonify({'updated': False})

@app.route('/admin/users', methods=['GET'])
@jwt_required()
def get_all_users():
    all_users = users.find()
    return jsonify([{**user, '_id': str(user['_id'])} for user in all_users])

@app.route('/flights/<id>', methods=['DELETE'])
@jwt_required()
def delete_flight(id):
    result = flights.delete_one({'_id': ObjectId(id)})
    return jsonify({'deleted': result.deleted_count > 0})

@app.route('/admin/assign-flight', methods=['POST'])
@jwt_required()
def assign_flight():
    data = request.json
    user_id = data.get('userId')
    flight_id = data.get('flightId')
    user = users.find_one({'_id': ObjectId(user_id)})
    if user:
        users.update_one({'_id': ObjectId(user_id)}, {'$addToSet': {'assigned_flights': flight_id}})
        return jsonify({'msg': 'Flight assigned successfully'})
    return jsonify({'msg': 'User not found'}), 404

@app.route('/users', methods=['GET'])
@jwt_required()
def get_users():
    all_users = users.find()
    return jsonify([{**user, '_id': str(user['_id'])} for user in all_users])

def create_notification(flight):
    users_to_notify = users.find({'assigned_flights': flight['flight_id']})
    for user in users_to_notify:
        message = f"Your flight {flight['flight_id']} is {flight['status']}. Departure gate: {flight['departure_gate']}."
        notification = {
            'flight_id': flight['flight_id'],
            'message': message,
            'timestamp': datetime.datetime.utcnow(),
            'method': 'SMS',
            'recipient': user['phone']
        }
        notifications.insert_one(notification)
        send_notification(user, notification)

def send_notification(user, notification):
    twilio_client.messages.create(
        body=notification['message'],
        from_=TWILIO_PHONE_NUMBER,
        to=user['phone']
    )
    email_message = Mail(
        from_email=Email(SENDGRID_SENDER_EMAIL),
        to_emails=To(user['email']),
        subject='Flight Status Update',
        html_content=Content('text/html', f"<strong>{notification['message']}</strong>")
    )
    sg.send(email_message)
    if 'fcm_token' in user:
        firebase_message = messaging.Message(
            notification=messaging.Notification(
                title='Flight Status Update',
                body=notification['message']
            ),
            token=user['fcm_token']
        )
        messaging.send(firebase_message)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))

# from flask import Flask, request, jsonify
# from flask_cors import CORS
# from pymongo import MongoClient
# from bson import ObjectId
# from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
# import datetime
# import os
# from dotenv import load_dotenv

# load_dotenv()

# app = Flask(__name__)
# app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET_KEY')
# jwt = JWTManager(app)
# CORS(app)

# client = MongoClient(os.getenv('MONGO_URI'))
# db = client[os.getenv('DB_NAME')]
# flights = db.flights
# notifications = db.notifications
# users = db.users

# ADMIN_CREDENTIALS = {
#     'username': os.getenv('ADMIN_USERNAME'),
#     'password': os.getenv('ADMIN_PASSWORD')
# }

# @app.route('/api', methods=['GET'])
# def home():
#     return "Welcome to the Flight Status API!"

# @app.route('/api/admin/login', methods=['POST'])
# def admin_login():
#     data = request.json
#     if data['username'] == ADMIN_CREDENTIALS['username'] and data['password'] == ADMIN_CREDENTIALS['password']:
#         access_token = create_access_token(identity={'username': data['username']})
#         return jsonify(access_token=access_token)
#     return jsonify({'msg': 'Invalid credentials'}), 401

# @app.route('/api/user/register', methods=['POST'])
# def user_register():
#     data = request.json
#     existing_user = users.find_one({'username': data['username']})
#     if existing_user:
#         return jsonify({'msg': 'Username already exists'}), 409
#     users.insert_one({
#         'username': data['username'],
#         'email': data['email'],
#         'phone': data['phone'],
#         'password': data['password'],
#         'assigned_flights': []
#     })
#     return jsonify({'msg': 'User registered successfully'}), 201

# @app.route('/api/user/login', methods=['POST'])
# def user_login():
#     data = request.json
#     user = users.find_one({'username': data['username'], 'password': data['password']})
#     if user:
#         access_token = create_access_token(identity={'username': user['username']})
#         return jsonify(access_token=access_token)
#     return jsonify({'msg': 'Invalid credentials'}), 401

# @app.route('/api/flights', methods=['POST'])
# @jwt_required()
# def add_flight():
#     flight_data = request.json
#     result = flights.insert_one(flight_data)
#     return jsonify({'id': str(result.inserted_id)})

# @app.route('/api/flights', methods=['GET'])
# @jwt_required()
# def get_flights():
#     user_identity = get_jwt_identity()
#     user = users.find_one({'username': user_identity['username']})
#     if user:
#         all_flights = flights.find({'flight_id': {'$in': user['assigned_flights']}})
#         return jsonify([{**flight, '_id': str(flight['_id'])} for flight in all_flights])
#     return jsonify({'msg': 'User not found'}), 404

# @app.route('/api/admin/flights', methods=['GET'])
# @jwt_required()
# def get_all_flights():
#     all_flights = flights.find()
#     return jsonify([{**flight, '_id': str(flight['_id'])} for flight in all_flights])

# @app.route('/api/flights/<id>', methods=['PUT'])
# @jwt_required()
# def update_flight(id):
#     updates = request.json
#     if '_id' in updates:
#         del updates['_id']
#     result = flights.update_one({'_id': ObjectId(id)}, {'$set': updates})
#     if result.modified_count > 0:
#         flight = flights.find_one({'_id': ObjectId(id)})
#         return jsonify({'updated': True})
#     return jsonify({'updated': False})

# @app.route('/api/admin/users', methods=['GET'])
# @jwt_required()
# def get_all_users():
#     all_users = users.find()
#     return jsonify([{**user, '_id': str(user['_id'])} for user in all_users])

# @app.route('/api/flights/<id>', methods=['DELETE'])
# @jwt_required()
# def delete_flight(id):
#     result = flights.delete_one({'_id': ObjectId(id)})
#     return jsonify({'deleted': result.deleted_count > 0})

# @app.route('/api/admin/assign-flight', methods=['POST'])
# @jwt_required()
# def assign_flight():
#     data = request.json
#     user_id = data.get('userId')
#     flight_id = data.get('flightId')
#     user = users.find_one({'_id': ObjectId(user_id)})
#     if user:
#         users.update_one({'_id': ObjectId(user_id)}, {'$addToSet': {'assigned_flights': flight_id}})
#         return jsonify({'msg': 'Flight assigned successfully'})
#     return jsonify({'msg': 'User not found'}), 404

# @app.route('/api/users', methods=['GET'])
# @jwt_required()
# def get_users():
#     all_users = users.find()
#     return jsonify([{**user, '_id': str(user['_id'])} for user in all_users])

# if __name__ == '__main__':
#     app.run(debug=True, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))