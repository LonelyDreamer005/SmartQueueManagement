from flask import Flask, request, jsonify, render_template, redirect, url_for, session, flash
from datetime import datetime
import sqlite3
import os
import threading
import time
import json
import random
from functools import wraps
import queue
import logging
from twilio.rest import Client
import secrets
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__, static_folder='static', template_folder='.')

# Set a secure secret key for session management
app.secret_key = secrets.token_hex(16)

# Twilio configuration (for SMS notifications)
TWILIO_SID = os.environ.get('TWILIO_SID', '')  # Set your Twilio SID as environment variable
TWILIO_TOKEN = os.environ.get('TWILIO_TOKEN', '')  # Set your Twilio token as environment variable
TWILIO_PHONE = os.environ.get('TWILIO_PHONE', '')  # Set your Twilio phone number

# Queue management using Python's thread-safe Queue (IPC concept)
token_queue = queue.Queue()
notification_queue = queue.Queue()
order_notification_queue = queue.Queue()

# Lock for thread synchronization (synchronization concept)
db_lock = threading.Lock()

# OS Concept: Memory Management - Efficient token storage
class TokenManager:
    def __init__(self, max_size=100):
        self.tokens = {}
        self.max_size = max_size
    
    def add_token(self, token_id, data):
        if len(self.tokens) >= self.max_size:
            # Remove oldest tokens if we reach capacity
            oldest = min(self.tokens.items(), key=lambda x: x[1]['time'])
            del self.tokens[oldest[0]]
        self.tokens[token_id] = data
    
    def get_token(self, token_id):
        return self.tokens.get(token_id)
    
    def remove_token(self, token_id):
        if token_id in self.tokens:
            del self.tokens[token_id]

token_manager = TokenManager()

# Estimated processing times - declare this before it's used
PROCESSING_TIME_PER_TOKEN = 3  # minutes

# Function to check if a column exists in a table
def column_exists(conn, table, column):
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table})")
    columns = [info[1] for info in cursor.fetchall()]
    return column in columns

# DB Setup
def init_db():
    with db_lock:
        with sqlite3.connect("queue.db") as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS tokens (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            name TEXT,
                            phone TEXT,
                            time TEXT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            priority INTEGER DEFAULT 0,
                            status TEXT DEFAULT 'waiting',
                            order_id INTEGER DEFAULT NULL)''')
            
            # Add phone column if it doesn't exist
            if not column_exists(conn, "tokens", "phone"):
                logger.info("Adding phone column to tokens table")
                conn.execute("ALTER TABLE tokens ADD COLUMN phone TEXT")
            
            # Add priority column if it doesn't exist
            if not column_exists(conn, "tokens", "priority"):
                logger.info("Adding priority column to tokens table")
                conn.execute("ALTER TABLE tokens ADD COLUMN priority INTEGER DEFAULT 0")
            
            # Add created_at column if it doesn't exist
            if not column_exists(conn, "tokens", "created_at"):
                logger.info("Adding created_at column to tokens table")
                conn.execute("ALTER TABLE tokens ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                
            # Add order_id column if it doesn't exist
            if not column_exists(conn, "tokens", "order_id"):
                logger.info("Adding order_id column to tokens table")
                conn.execute("ALTER TABLE tokens ADD COLUMN order_id INTEGER DEFAULT NULL")
            
            # Create a table for staff accounts
            conn.execute('''CREATE TABLE IF NOT EXISTS staff (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            username TEXT UNIQUE,
                            password TEXT,
                            is_admin INTEGER DEFAULT 0)''')
            
            # Add default admin if not exists
            if not conn.execute("SELECT * FROM staff WHERE username='admin'").fetchone():
                conn.execute("INSERT INTO staff (username, password, is_admin) VALUES (?, ?, ?)", 
                          ('admin', 'password123', 1))
                
            # Create stationary items table
            conn.execute('''CREATE TABLE IF NOT EXISTS stationary_items (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            name TEXT NOT NULL,
                            description TEXT,
                            price REAL NOT NULL,
                            quantity INTEGER NOT NULL,
                            image_url TEXT,
                            emoji TEXT
                        )''')
                
            # Add emoji column if it doesn't exist
            if not column_exists(conn, "stationary_items", "emoji"):
                logger.info("Adding emoji column to stationary_items table")
                conn.execute("ALTER TABLE stationary_items ADD COLUMN emoji TEXT")
                
                # Add default emojis to existing items
                default_emojis = {
                    'Notebook': '📓',
                    'Pencil': '✏️',
                    'Pen': '🖊️',
                    'Highlighter': '🖌️',
                    'Eraser': '🧽',
                    'Ruler': '📏',
                    'Sticky': '📝',
                    'Stapler': '📎',
                    'Scissors': '✂️',
                    'Glue': '🧴',
                    'File Folder': '📁',
                    'Calculator': '🧮',
                    'Printout': '🖨️'
                }
                
                # Update existing items with emojis
                for item_name, emoji in default_emojis.items():
                    conn.execute(
                        "UPDATE stationary_items SET emoji = ? WHERE name LIKE ? AND emoji IS NULL", 
                        (emoji, f"%{item_name}%")
                    )
                    
                # Set default emoji for any remaining items
                conn.execute("UPDATE stationary_items SET emoji = '📦' WHERE emoji IS NULL")
                
            # Create orders table
            conn.execute('''CREATE TABLE IF NOT EXISTS orders (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER,
                            total_amount REAL NOT NULL,
                            status TEXT DEFAULT 'pending',
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            payment_method TEXT,
                            payment_status TEXT DEFAULT 'pending',
                            FOREIGN KEY (user_id) REFERENCES tokens(id)
                        )''')
                
            # Create order items table
            conn.execute('''CREATE TABLE IF NOT EXISTS order_items (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            order_id INTEGER,
                            item_id INTEGER,
                            quantity INTEGER NOT NULL,
                            price REAL NOT NULL,
                            FOREIGN KEY (order_id) REFERENCES orders(id),
                            FOREIGN KEY (item_id) REFERENCES stationary_items(id)
                        )''')
            
            # Create transactions table for tracking all payments
            conn.execute('''CREATE TABLE IF NOT EXISTS transactions (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            order_id INTEGER,
                            token_id INTEGER,
                            customer_name TEXT,
                            amount REAL NOT NULL,
                            payment_method TEXT,
                            payment_status TEXT,
                            transaction_date TIMESTAMP,
                            FOREIGN KEY (order_id) REFERENCES orders(id),
                            FOREIGN KEY (token_id) REFERENCES tokens(id)
                        )''')
                
            # Check if stationary items table is empty and add sample data
            if not conn.execute("SELECT COUNT(*) FROM stationary_items").fetchone()[0]:
                sample_items = [
                    ('Notebook - A4 Size', 'Spiral bound notebook with 100 pages', 299, 50, '/static/images/notebook.jpg', '📓'),
                    ('Pencil Pack', 'Pack of 10 HB pencils', 200, 100, '/static/images/pencils.jpg', '✏️'),
                    ('Ballpoint Pens', 'Pack of 5 blue ballpoint pens', 250, 75, '/static/images/pens.jpg', '🖊️'),
                    ('Highlighter Set', 'Pack of 4 highlighters in different colors', 399, 40, '/static/images/highlighters.jpg', '🖌️'),
                    ('Eraser', 'High quality rubber eraser', 75, 200, '/static/images/eraser.jpg', '🧽'),
                    ('Ruler - 30cm', 'Transparent plastic ruler', 125, 80, '/static/images/ruler.jpg', '📏'),
                    ('Sticky Notes', 'Pack of colorful sticky notes', 225, 60, '/static/images/sticky_notes.jpg', '📝'),
                    ('Stapler', 'Metal stapler with 100 staples', 450, 30, '/static/images/stapler.jpg', '📎'),
                    ('Scissors', 'Stainless steel scissors', 299, 40, '/static/images/scissors.jpg', '✂️'),
                    ('Glue Stick', 'Non-toxic glue stick', 99, 90, '/static/images/glue.jpg', '🧴'),
                    ('File Folder', 'Pack of 5 colorful file folders', 350, 45, '/static/images/folders.jpg', '📁'),
                    ('Calculator', 'Scientific calculator', 950, 20, '/static/images/calculator.jpg', '🧮'),
                    ('Printout (Black & White)', 'Per page, A4 size', 5, 1000, '/static/images/printout_bw.jpg', '🖨️'),
                    ('Printout (Color)', 'Per page, A4 size', 15, 1000, '/static/images/printout_color.jpg', '🖨️'),
                    ('Spiral Binding', 'For documents up to 100 pages', 50, 200, '', '📎')
                ]
                
                conn.executemany(
                    "INSERT INTO stationary_items (name, description, price, quantity, image_url, emoji) VALUES (?, ?, ?, ?, ?, ?)",
                    sample_items
                )
                
                conn.commit()
                logger.info("Database initialized with sample stationary items.")
                
init_db()

# Authentication decorator
def staff_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'staff_id' not in session:
            return redirect(url_for('staff_login'))
        return f(*args, **kwargs)
    return decorated_function

# Admin decorator
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'staff_id' not in session or not session.get('is_admin'):
            flash("Admin access required", "error")
            return redirect(url_for('staff_login'))
        return f(*args, **kwargs)
    return decorated_function

# Function to send SMS notifications
def send_sms_notification(phone, message):
    if not TWILIO_SID or not TWILIO_TOKEN or not TWILIO_PHONE:
        logger.warning("Twilio credentials not set. SMS not sent.")
        return False
    
    # Check if phone number is in valid format (should start with + and country code)
    if not phone or not phone.startswith('+'):
        logger.warning(f"Invalid phone number format: {phone}. SMS not sent.")
        return False
    
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(
            body=message,
            from_=TWILIO_PHONE,
            to=phone
        )
        logger.info(f"SMS sent to {phone}")
        return True
    except Exception as e:
        logger.error(f"Failed to send SMS: {str(e)}")
        return False

# Background thread for notification processing (multithreading concept)
def notification_worker():
    while True:
        try:
            notification = notification_queue.get(timeout=1)
            send_sms_notification(notification['phone'], notification['message'])
            notification_queue.task_done()
        except queue.Empty:
            time.sleep(1)
        except Exception as e:
            logger.error(f"Error in notification worker: {str(e)}")
            time.sleep(1)

# Background thread for order ready notifications
def order_notification_worker():
    while True:
        try:
            notification = order_notification_queue.get(timeout=1)
            send_sms_notification(notification['phone'], notification['message'])
            order_notification_queue.task_done()
        except queue.Empty:
            time.sleep(1)
        except Exception as e:
            logger.error(f"Error in order notification worker: {str(e)}")
            time.sleep(1)

# Start notification threads
notification_thread = threading.Thread(target=notification_worker, daemon=True)
notification_thread.start()

order_notification_thread = threading.Thread(target=order_notification_worker, daemon=True)
order_notification_thread.start()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/items')
def items():
    return render_template('items.html')

@app.route('/checkout')
def checkout():
    return render_template('checkout.html')

@app.route('/generate_token', methods=['POST'])
def generate_token():
    try:
        name = request.json.get('name')
        phone = request.json.get('phone', '')
        priority = request.json.get('priority', 0)
        
        if not name:
            return jsonify({'error': 'Name is required'}), 400
            
        current_time = datetime.now()
        time_str = current_time.strftime("%H:%M:%S")
        
        with db_lock:  # Thread synchronization
            with sqlite3.connect("queue.db") as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT INTO tokens (name, phone, time, priority, status) VALUES (?, ?, ?, ?, ?)", 
                           (name, phone, time_str, priority, 'waiting'))
                token_id = cursor.lastrowid
        
        # Add to token manager (memory management)
        token_manager.add_token(token_id, {
            'name': name,
            'phone': phone,
            'time': time_str,
            'created_at': current_time.timestamp(),
            'priority': priority,
            'status': 'waiting'
        })
        
        # Add to notification queue for later processing
        if phone:
            message = f"Hello {name}, your token #{token_id} has been generated. Current estimated wait time: {get_estimated_wait_time(token_id)} minutes."
            notification_queue.put({'phone': phone, 'message': message})
        
        # Get position in queue
        position = get_position_in_queue(token_id)
        estimated_wait = get_estimated_wait_time(token_id)
        
        return jsonify({
            'token_id': token_id,
            'name': name,
            'time': time_str,
            'position': position,
            'estimated_wait': estimated_wait,
            'message': 'Token generated successfully'
        })
    except Exception as e:
        logger.error(f"Error generating token: {str(e)}")
        return jsonify({'error': 'Failed to generate token'}), 500

def get_position_in_queue(token_id):
    """Get the current position of a token in the queue"""
    try:
        with sqlite3.connect("queue.db") as conn:
            cursor = conn.cursor()
            # Get all active tokens ordered by creation time
            active_tokens = cursor.execute(
                "SELECT id FROM tokens WHERE status = 'waiting' ORDER BY created_at ASC"
            ).fetchall()
            
            # Find position of the given token
            for i, (tid,) in enumerate(active_tokens):
                if tid == token_id:
                    return i + 1  # Position is 1-indexed
            
            return -1  # Token not found or not waiting
    except Exception as e:
        logger.error(f"Error getting queue position: {str(e)}")
        return -1

def get_estimated_wait_time(token_id):
    """Estimate wait time in minutes for a token"""
    try:
        position = get_position_in_queue(token_id)
        if position <= 0:
            return 0
        
        # Estimate based on average processing time
        # Assume each customer takes about 5 minutes to serve
        avg_time_per_customer = 5
        
        return position * avg_time_per_customer
    except Exception as e:
        logger.error(f"Error calculating wait time: {str(e)}")
        return -1

@app.route('/queue_status', methods=['GET'])
def queue_status():
    try:
        with sqlite3.connect("queue.db") as conn:
            conn.row_factory = sqlite3.Row
            tokens = conn.execute(
                "SELECT id, name, time, priority, status FROM tokens WHERE status='waiting' ORDER BY priority DESC, created_at ASC"
            ).fetchall()
        
        result = []
        for token in tokens:
            position = get_position_in_queue(token['id'])
            estimated_wait = position * PROCESSING_TIME_PER_TOKEN
            
            result.append({
                'id': token['id'],
                'name': token['name'],
                'time': token['time'],
                'priority': token['priority'],
                'position': position,
                'estimated_wait': estimated_wait
            })
        
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error getting queue status: {str(e)}")
        return jsonify({'error': 'Failed to get queue status'}), 500

@app.route('/token/<int:token_id>', methods=['GET'])
def get_token_status(token_id):
    try:
        with sqlite3.connect("queue.db") as conn:
            conn.row_factory = sqlite3.Row
            token = conn.execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone()
            
        if not token:
            return jsonify({'error': 'Token not found'}), 404
        
        position = get_position_in_queue(token_id) if token['status'] == 'waiting' else 0
        estimated_wait = position * PROCESSING_TIME_PER_TOKEN if position > 0 else 0
        
        return jsonify({
            'id': token['id'],
            'name': token['name'],
            'time': token['time'],
            'status': token['status'],
            'position': position,
            'estimated_wait': estimated_wait,
            'priority': token['priority']
        })
    except Exception as e:
        logger.error(f"Error getting token status: {str(e)}")
        return jsonify({'error': 'Failed to get token status'}), 500

# Get all stationary items
@app.route('/api/stationary_items', methods=['GET'])
def get_stationary_items():
    try:
        with sqlite3.connect("queue.db") as conn:
            conn.row_factory = sqlite3.Row
            items = conn.execute("SELECT * FROM stationary_items ORDER BY name").fetchall()
            
        result = []
        for item in items:
            result.append({
                'id': item['id'],
                'name': item['name'],
                'description': item['description'],
                'price': f"₹{item['price']}",
                'quantity': item['quantity'],
                'image_url': item['image_url'],
                'emoji': item['emoji']
            })
            
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error getting stationary items: {str(e)}")
        return jsonify({'error': 'Failed to get stationary items'}), 500

# Place a new order
@app.route('/api/place_order', methods=['POST'])
def place_order():
    try:
        data = request.json
        
        customer = data.get('customer', {})
        items = data.get('items', [])
        payment_method = data.get('payment_method', 'cash')
        payment_status = data.get('payment_status', 'pending')
        
        name = customer.get('name')
        phone = customer.get('phone')
        email = customer.get('email')
        
        if not name or not items:
            return jsonify({'error': 'Name and items are required'}), 400
        
        with db_lock:
            with sqlite3.connect("queue.db") as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                # Create token first
                current_time = datetime.now()
                time_str = current_time.strftime("%H:%M:%S")
                
                cursor.execute(
                    "INSERT INTO tokens (name, phone, time, status) VALUES (?, ?, ?, ?)",
                    (name, phone, time_str, 'waiting')
                )
                token_id = cursor.lastrowid
                
                # Calculate total amount
                total_amount = sum(item['price'] * item['quantity'] for item in items)
                
                # Create order
                cursor.execute(
                    "INSERT INTO orders (user_id, total_amount, payment_method, payment_status) VALUES (?, ?, ?, ?)",
                    (token_id, total_amount, payment_method, payment_status)
                )
                order_id = cursor.lastrowid
                
                # Update token with order_id
                cursor.execute("UPDATE tokens SET order_id = ? WHERE id = ?", (order_id, token_id))
                
                # Add order items and collect names for notification
                item_details = []
                for item in items:
                    cursor.execute(
                        "INSERT INTO order_items (order_id, item_id, quantity, price) VALUES (?, ?, ?, ?)",
                        (order_id, item['id'], item['quantity'], item['price'])
                    )
                    
                    # Get item name for notification
                    item_name = cursor.execute(
                        "SELECT name FROM stationary_items WHERE id = ?", 
                        (item['id'],)
                    ).fetchone()['name']
                    
                    item_details.append(f"{item['quantity']}x {item_name}")
                    
                    # Update inventory
                    cursor.execute(
                        "UPDATE stationary_items SET quantity = quantity - ? WHERE id = ?",
                        (item['quantity'], item['id'])
                    )
                
                # Record transaction
                cursor.execute(
                    """INSERT INTO transactions 
                       (order_id, token_id, customer_name, amount, payment_method, payment_status, transaction_date) 
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (order_id, token_id, name, total_amount, payment_method, payment_status, current_time)
                )
                
                conn.commit()
                
                # Send notification
                if phone:
                    if payment_method == 'cash':
                        # For pay-at-counter orders, include payment amount and items
                        items_list = ", ".join(item_details[:3])
                        if len(item_details) > 3:
                            items_list += f" and {len(item_details) - 3} more items"
                            
                        message = f"Hello {name}, your order #{order_id} has been placed. Please pay ₹{total_amount:.2f} at the counter for: {items_list}. Your token number is #{token_id}. You'll receive a message when your order is ready."
                    else:
                        message = f"Hello {name}, your order #{order_id} has been placed and payment of ₹{total_amount:.2f} processed. You'll receive another message when your items are ready for collection. Your token number is #{token_id}."
                    
                    notification_queue.put({'phone': phone, 'message': message})
                
                # Add to token manager
                token_manager.add_token(token_id, {
                    'name': name,
                    'phone': phone,
                    'time': time_str,
                    'created_at': current_time.timestamp(),
                    'status': 'waiting',
                    'order_id': order_id
                })
                
                return jsonify({
                    'message': 'Order placed successfully',
                    'order_id': order_id,
                    'token_id': token_id
                })
                
    except Exception as e:
        logger.error(f"Error placing order: {str(e)}")
        return jsonify({'error': 'Failed to place order'}), 500

# Staff portal routes
@app.route('/staff/login', methods=['GET', 'POST'])
def staff_login():
    if request.method == 'GET':
        return render_template('staff_login.html')
    
    username = request.form.get('username')
    password = request.form.get('password')
    
    try:
        with sqlite3.connect("queue.db") as conn:
            conn.row_factory = sqlite3.Row
            staff = conn.execute("SELECT * FROM staff WHERE username=? AND password=?", 
                               (username, password)).fetchone()
        
        if staff:
            session['staff_id'] = staff['id']
            session['username'] = staff['username']
            session['is_admin'] = bool(staff['is_admin'])
            return redirect(url_for('staff_dashboard'))
        else:
            flash("Invalid credentials", "error")
            return redirect(url_for('staff_login'))
    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        flash("An error occurred during login", "error")
        return redirect(url_for('staff_login'))

@app.route('/staff/logout')
def staff_logout():
    session.clear()
    return redirect(url_for('staff_login'))

@app.route('/staff/dashboard')
@staff_required
def staff_dashboard():
    return render_template('staff_dashboard.html')

# Get order details
@app.route('/api/order/<int:order_id>', methods=['GET'])
@staff_required
def get_order_details(order_id):
    try:
        with sqlite3.connect("queue.db") as conn:
            conn.row_factory = sqlite3.Row
            
            # Get order
            order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not order:
                return jsonify({'error': 'Order not found'}), 404
            
            # Get token/customer details
            token = conn.execute("SELECT * FROM tokens WHERE id = ?", (order['user_id'],)).fetchone()
            
            # Get order items
            items = conn.execute("""
                SELECT oi.*, si.name, si.description 
                FROM order_items oi 
                JOIN stationary_items si ON oi.item_id = si.id 
                WHERE oi.order_id = ?
            """, (order_id,)).fetchall()
            
            order_items = []
            for item in items:
                order_items.append({
                    'id': item['id'],
                    'item_id': item['item_id'],
                    'name': item['name'],
                    'description': item['description'],
                    'quantity': item['quantity'],
                    'price': f"₹{item['price']}",
                    'total': f"₹{item['quantity'] * item['price']}"
                })
            
            # Construct response
            result = {
                'order': {
                    'id': order['id'],
                    'total_amount': f"₹{order['total_amount']}",
                    'status': order['status'],
                    'created_at': order['created_at'],
                    'payment_method': order['payment_method'],
                    'payment_status': order['payment_status']
                },
                'customer': {
                    'id': token['id'],
                    'name': token['name'],
                    'phone': token['phone'],
                    'time': token['time'],
                    'status': token['status']
                },
                'items': order_items
            }
            
            return jsonify(result)
    except Exception as e:
        logger.error(f"Error getting order details: {str(e)}")
        return jsonify({'error': 'Failed to get order details'}), 500

@app.route('/staff/serve_next', methods=['POST'])
@staff_required
def serve_next_token():
    try:
        with db_lock:
            with sqlite3.connect("queue.db") as conn:
                # Process Scheduling: Priority + FCFS (First Come First Serve)
                conn.row_factory = sqlite3.Row
                token = conn.execute(
                    "SELECT * FROM tokens WHERE status='waiting' ORDER BY priority DESC, created_at ASC LIMIT 1"
                ).fetchone()
                
                if not token:
                    return jsonify({'message': 'No tokens in queue'}), 404
                
                token_id = token['id']
                conn.execute("UPDATE tokens SET status='serving' WHERE id=?", (token_id,))
                
                # If there's an order associated with this token, get the details
                order_details = None
                if token['order_id']:
                    order = conn.execute("SELECT * FROM orders WHERE id=?", (token['order_id'],)).fetchone()
                    if order:
                        order_details = {
                            'id': order['id'],
                            'total_amount': f"₹{order['total_amount']}",
                            'payment_method': order['payment_method'],
                            'payment_status': order['payment_status']
                        }
                
                # Notify customer if phone number is available
                if token['phone']:
                    if token['order_id']:
                        message = f"Hello {token['name']}, your stationery order is ready for collection! Please proceed to the counter with token #{token_id}."
                    else:
                        message = f"Hello {token['name']}, it's your turn now! Please proceed to the counter."
                    notification_queue.put({'phone': token['phone'], 'message': message})
                
                response_data = {
                    'message': 'Now serving token',
                    'token': {
                        'id': token['id'],
                        'name': token['name'],
                        'time': token['time']
                    }
                }
                
                if order_details:
                    response_data['order'] = order_details
                
                return jsonify(response_data)
    except Exception as e:
        logger.error(f"Error serving next token: {str(e)}")
        return jsonify({'error': 'Failed to serve next token'}), 500

@app.route('/staff/complete_token/<int:token_id>', methods=['POST'])
@staff_required
def complete_token(token_id):
    try:
        with db_lock:
            with sqlite3.connect("queue.db") as conn:
                conn.row_factory = sqlite3.Row
                
                # Get token info first for notification
                token = conn.execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone()
                
                # Update token status
                conn.execute("UPDATE tokens SET status='completed' WHERE id=?", (token_id,))
                
                # If there's an order associated, update its status too
                if token and token['order_id']:
                    conn.execute("UPDATE orders SET status='completed' WHERE id=?", (token['order_id'],))
        
        # Clean up memory
        token_manager.remove_token(token_id)
        
        return jsonify({'message': 'Token marked as completed'})
    except Exception as e:
        logger.error(f"Error completing token: {str(e)}")
        return jsonify({'error': 'Failed to complete token'}), 500

@app.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
def admin_settings():
    global PROCESSING_TIME_PER_TOKEN  # Declare global variable at the beginning of the function
    
    if request.method == 'GET':
        # Get current queue statistics
        with sqlite3.connect("queue.db") as conn:
            conn.row_factory = sqlite3.Row
            
            # Get tokens generated today
            today = datetime.now().strftime('%Y-%m-%d')
            tokens_today = conn.execute(
                "SELECT COUNT(*) as count FROM tokens WHERE date(created_at) = date('now')"
            ).fetchone()['count']
            
            # Get current queue size
            queue_size = conn.execute(
                "SELECT COUNT(*) as count FROM tokens WHERE status='waiting'"
            ).fetchone()['count']
            
            # Get priority tokens
            priority_tokens = conn.execute(
                "SELECT COUNT(*) as count FROM tokens WHERE status='waiting' AND priority > 0"
            ).fetchone()['count']
            
            # Calculate average wait time (simplified version)
            avg_wait_time = PROCESSING_TIME_PER_TOKEN * (queue_size / 2 if queue_size > 0 else 0)

            # Get inventory stats
            total_items = conn.execute(
                "SELECT COUNT(*) as count FROM stationary_items"
            ).fetchone()['count']
            
            low_stock_items = conn.execute(
                "SELECT COUNT(*) as count FROM stationary_items WHERE quantity < 10"
            ).fetchone()['count']
            
            total_orders = conn.execute(
                "SELECT COUNT(*) as count FROM orders"
            ).fetchone()['count']
            
            orders_today = conn.execute(
                "SELECT COUNT(*) as count FROM orders WHERE date(created_at) = date('now')"
            ).fetchone()['count']
            
            pending_orders = conn.execute(
                "SELECT COUNT(*) as count FROM orders WHERE status = 'pending'"
            ).fetchone()['count']
            
        # Pass all variables to the template
        return render_template('admin_settings.html',
            processing_time=PROCESSING_TIME_PER_TOKEN,
            tokens_today=tokens_today,
            queue_size=queue_size,
            priority_tokens=priority_tokens,
            avg_wait_time=round(avg_wait_time, 1),
            twilio_sid=bool(TWILIO_SID),
            twilio_token=bool(TWILIO_TOKEN),
            twilio_phone=TWILIO_PHONE,
            total_items=total_items,
            low_stock_items=low_stock_items,
            total_orders=total_orders,
            orders_today=orders_today,
            pending_orders=pending_orders
        )
    
    # Handle settings update
    try:
        processing_time = int(request.form.get('processing_time', PROCESSING_TIME_PER_TOKEN))
        PROCESSING_TIME_PER_TOKEN = processing_time
        
        flash("Settings updated successfully", "success")
        return redirect(url_for('admin_settings'))
    except Exception as e:
        logger.error(f"Error updating settings: {str(e)}")
        flash("Failed to update settings", "error")
        return redirect(url_for('admin_settings'))

@app.route('/api/current_serving', methods=['GET'])
def get_current_serving():
    try:
        with sqlite3.connect("queue.db") as conn:
            conn.row_factory = sqlite3.Row
            token = conn.execute("SELECT id, name FROM tokens WHERE status='serving' ORDER BY id DESC LIMIT 1").fetchone()
        
        if token:
            return jsonify({'id': token['id'], 'name': token['name']})
        else:
            return jsonify({'message': 'No token currently being served'}), 404
    except Exception as e:
        logger.error(f"Error getting current token: {str(e)}")
        return jsonify({'error': 'Failed to get current token'}), 500

@app.route('/display')
def public_display():
    """Public display board for the queue"""
    return render_template('display.html')

# Get transactions for admin dashboard
@app.route('/api/transactions', methods=['GET'])
@admin_required
def get_transactions():
    try:
        with sqlite3.connect("queue.db") as conn:
            conn.row_factory = sqlite3.Row
            transactions = conn.execute("""
                SELECT * FROM transactions 
                ORDER BY transaction_date DESC
                LIMIT 100
            """).fetchall()
            
        result = []
        for transaction in transactions:
            result.append({
                'id': transaction['id'],
                'order_id': transaction['order_id'],
                'token_id': transaction['token_id'],
                'customer_name': transaction['customer_name'],
                'amount': f"₹{transaction['amount']}",
                'payment_method': transaction['payment_method'],
                'payment_status': transaction['payment_status'],
                'transaction_date': transaction['transaction_date']
            })
            
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error getting transactions: {str(e)}")
        return jsonify({'error': 'Failed to get transactions'}), 500

@app.route('/api/continue_waiting/<int:token_id>', methods=['POST'])
def continue_waiting(token_id):
    try:
        # Get decision from request
        data = request.json
        continue_waiting = data.get('continue_waiting', True)
        
        with sqlite3.connect("queue.db") as conn:
            # Check if token exists and is waiting
            token = conn.execute("SELECT * FROM tokens WHERE id=? AND status='waiting'", (token_id,)).fetchone()
            
            if not token:
                return jsonify({'error': 'Token not found or not in waiting status'}), 404
                
            if not continue_waiting:
                # User doesn't want to continue waiting
                conn.execute("UPDATE tokens SET status='cancelled' WHERE id=?", (token_id,))
                conn.commit()
                
                return jsonify({
                    'message': 'Token cancelled successfully',
                    'token_id': token_id
                })
            else:
                # User wants to continue waiting
                # Update the timestamp to show continued interest
                conn.execute("UPDATE tokens SET last_confirmation=CURRENT_TIMESTAMP WHERE id=?", (token_id,))
                conn.commit()
                
                # Return updated position and wait time
                position = get_position_in_queue(token_id)
                estimated_wait = get_estimated_wait_time(token_id)
                
                return jsonify({
                    'message': 'Thank you for your patience',
                    'token_id': token_id,
                    'position': position,
                    'estimated_wait': estimated_wait
                })
                
    except Exception as e:
        logger.error(f"Error processing continue waiting: {str(e)}")
        return jsonify({'error': 'Failed to process continue waiting request'}), 500

@app.route('/token/<int:token_id>/continue', methods=['POST'])
def continue_token(token_id):
    try:
        continue_waiting = request.json.get('continue', False)
        
        with db_lock:
            with sqlite3.connect("queue.db") as conn:
                # Get the token info first
                token = conn.execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone()
                
                if not token:
                    return jsonify({'error': 'Token not found'}), 404
                
                if continue_waiting:
                    # User wants to continue waiting - no change to the token status
                    message = f"You will maintain your position in the queue. Your estimated wait time is {get_estimated_wait_time(token_id)} minutes."
                    
                    # Send a notification if phone number is available
                    if token[2]:  # phone number is in index 2
                        notification_message = f"Thank you for confirming. Your token #{token_id} remains active with an estimated wait time of {get_estimated_wait_time(token_id)} minutes."
                        notification_queue.put({'phone': token[2], 'message': notification_message})
                else:
                    # User wants to cancel their token
                    conn.execute("UPDATE tokens SET status='cancelled' WHERE id=?", (token_id,))
                    message = "Your token has been cancelled. Thank you for using our service."
                    
                    # Send a notification if phone number is available
                    if token[2]:  # phone number is in index 2
                        notification_message = f"Your token #{token_id} has been cancelled. Thank you for using our service."
                        notification_queue.put({'phone': token[2], 'message': notification_message})
        
        return jsonify({
            'success': True,
            'message': message,
            'continue': continue_waiting
        })
    except Exception as e:
        logger.error(f"Error updating token continuation status: {str(e)}")
        return jsonify({'error': 'Failed to update token status'}), 500

if __name__ == '__main__':
    # For debugging, you can enable debug mode, but for production, set debug=False
    app.run(debug=True, threaded=True)
