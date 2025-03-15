from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

# Initialize Flask app
app = Flask(__name__)
app.app_context().push()

# Database configuration
app.config['SQLALCHEMY_DATABASE_URI'] = (
    f"mysql+pymysql://{os.getenv('MYSQL_USER')}:{os.getenv('MYSQL_PASSWORD')}@"
    f"{os.getenv('MYSQL_HOST')}:{os.getenv('MYSQL_PORT')}/{os.getenv('MYSQL_DATABASE')}"
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 10,
    'pool_recycle': 280,  # Close connections after 280 seconds to prevent timeout
    'pool_pre_ping': True,  # Test connections before using them
}


# Initialize SQLAlchemy
dbSql = SQLAlchemy(app)


class Users(dbSql.Model):
    id = dbSql.Column(dbSql.Integer, primary_key=True)
    username = dbSql.Column(dbSql.String(20), unique=True, nullable=False)
    email = dbSql.Column(dbSql.String(120), unique=True, nullable=True)
    discord_id = dbSql.Column(dbSql.String(25), unique=True, nullable=True)
    icon_url = dbSql.Column(dbSql.String(100), nullable=True)
    date_created = dbSql.Column(dbSql.DateTime, nullable=False, default=datetime.utcnow)
    isAdmin = dbSql.Column(dbSql.Boolean, default=False, nullable=False)
    isStaff = dbSql.Column(dbSql.Boolean, default=False, nullable=False)
    isBanned = dbSql.Column(dbSql.Boolean, default=False, nullable=False)
    isPremium = dbSql.Column(dbSql.Boolean, default=False, nullable=False)
    
    def __repr__(self):
        return f"User: '{self.username}'"

# Server Model
class Server(dbSql.Model):
    __tablename__ = 'server'
    id = dbSql.Column(dbSql.Integer, primary_key=True)
    discord_id = dbSql.Column(dbSql.String(20), nullable=False, unique=True)
    server_name = dbSql.Column(dbSql.String(100), nullable=False)
    isPremium = dbSql.Column(dbSql.Boolean, default=False, nullable=False)
    member_count = dbSql.Column(dbSql.Integer, nullable=False, default=0)
    channel_count = dbSql.Column(dbSql.Integer, nullable=False, default=0)
    icon_url = dbSql.Column(dbSql.String(100), nullable=True)
    server_admin_id = dbSql.Column(dbSql.Integer, dbSql.ForeignKey('users.id'), nullable=False)
    server_admin = dbSql.relationship('Users', backref=dbSql.backref('server', lazy=True))
    voice_time = dbSql.Column(dbSql.Float, nullable=False, default=0)
    download_count = dbSql.Column(dbSql.Integer, nullable=False, default=0)

    def __repr__(self):
        return f"<Server(name='{self.server_name}', discord_id='{self.discord_id}')>"

# Subscriptions Model
class Subscriptions(dbSql.Model):
    __tablename__ = 'subscriptions'
    id = dbSql.Column(dbSql.Integer, primary_key=True)
    user_id = dbSql.Column(dbSql.Integer, dbSql.ForeignKey('users.id'), nullable=False)
    user = dbSql.relationship('Users', backref=dbSql.backref('subscriptions', lazy=True))
    tier = dbSql.Column(dbSql.Integer, nullable=False, default=0)
    server_id = dbSql.Column(dbSql.Integer, dbSql.ForeignKey('server.id'), nullable=False)
    server = dbSql.relationship('Server', backref=dbSql.backref('subscriptions', lazy=True))
    service = dbSql.Column(dbSql.Integer, nullable=False)
    date_created = dbSql.Column(dbSql.DateTime, nullable=False, default=datetime.utcnow)
    expiry_date = dbSql.Column(dbSql.DateTime, nullable=True)

    def __repr__(self):
        return f"<Subscription(service='{self.service}', user_id={self.user_id}, server_id={self.server_id})>"
