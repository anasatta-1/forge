from flask import Flask, request, jsonify
import json

app = Flask(__name__)

# Load notes from file
try:
    with open('notes.json') as f:
        notes = json.load(f)
except FileNotFoundError:
    notes = []

# GET endpoint to retrieve all notes
@app.route('/notes', methods=['GET'])
def get_notes():
    return jsonify(notes)

# POST endpoint to create a new note
@app.route('/notes', methods=['POST'])
def create_note():
    new_note = request.json
    notes.append(new_note)
    with open('notes.json', 'w') as f:
        json.dump(notes, f)
    return jsonify(new_note), 201

if __name__ == '__main__':
    app.run(debug=True)