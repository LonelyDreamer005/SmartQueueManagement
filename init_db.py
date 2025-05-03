import sqlite3
import os

def init_database():
    # Connect to database
    with sqlite3.connect("queue.db") as conn:
        cursor = conn.cursor()
        
        # Create stationary items table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS stationary_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            price REAL NOT NULL,
            quantity INTEGER NOT NULL,
            image_url TEXT,
            emoji TEXT
        )
        ''')
        
        # Create orders table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            total_amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            payment_method TEXT,
            payment_status TEXT DEFAULT 'pending',
            FOREIGN KEY (user_id) REFERENCES tokens(id)
        )
        ''')
        
        # Create order items table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER,
            item_id INTEGER,
            quantity INTEGER NOT NULL,
            price REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id),
            FOREIGN KEY (item_id) REFERENCES stationary_items(id)
        )
        ''')
        
        # Check if stationary items table is empty
        cursor.execute("SELECT COUNT(*) FROM stationary_items")
        count = cursor.fetchone()[0]
        
        # If table is empty, add some sample stationary items with emojis
        if count == 0:
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
                ('Printout (Black & White)', 'Per page, A4 size', 5, 1000, None, '🖨️'),
                ('Printout (Color)', 'Per page, A4 size', 15, 1000, None, '🖨️'),
                ('Spiral Binding', 'For documents up to 100 pages', 50, 200, None, '📎')
            ]
            
            cursor.executemany(
                "INSERT INTO stationary_items (name, description, price, quantity, image_url, emoji) VALUES (?, ?, ?, ?, ?, ?)",
                sample_items
            )
            
            conn.commit()
            print("Database initialized with sample stationary items.")
        
        print("All database tables created successfully!")

if __name__ == "__main__":
    init_database()