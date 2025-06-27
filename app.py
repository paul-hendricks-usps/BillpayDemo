#!/usr/bin/env python
from flask import Flask, render_template, jsonify, request, redirect, url_for, flash, current_app
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask.cli import with_appcontext
from datetime import datetime, timedelta
import calendar
from calendar import monthrange
from sqlalchemy import or_, and_, extract, Date, cast, func
from datetime import datetime, timedelta, date
import dateutil.relativedelta
from dateutil.relativedelta import relativedelta
import re
import smtplib
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from collections import namedtuple
import secrets
import os
import logging
from logging import Handler
from logging.handlers import RotatingFileHandler

class MonthlyRotatingFileHandler(logging.Handler):
    def __init__(self, base_filename):
        super().__init__()
        self.base_filename = base_filename
        self.current_month = datetime.now().strftime('%Y-%m')
        self.stream = open(self.base_filename, 'a', encoding='utf-8')

    def emit(self, record):
        now_month = datetime.now().strftime('%Y-%m')
        if now_month != self.current_month:
            self.rotate_log(now_month)
        msg = self.format(record)
        self.stream.write(msg + '\n')
        self.stream.flush()

    def rotate_log(self, new_month):
        self.stream.close()
        rotated_name = f"{os.path.splitext(self.base_filename)[0]}-{self.current_month}.log"
        if os.path.exists(self.base_filename):
            os.rename(self.base_filename, rotated_name)
        self.stream = open(self.base_filename, 'a', encoding='utf-8')
        self.current_month = new_month

    def close(self):
        if self.stream:
            self.stream.close()
        super().close()


# Settings for the application, pay_period_days should be number of days in a pay period
# For a two week pay period that would be 13 days (14 days would include the next payday)
Settings = namedtuple("Settings", ["pay_period_days", "main_payday_name", "version"])
SETTINGS = Settings(13, 'USPS Payday', "1.0")

app = Flask(__name__)

# Logging setup
log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'billpay_app.log')
monthly_handler = MonthlyRotatingFileHandler(log_path)
monthly_handler.setLevel(logging.INFO)
formatter = logging.Formatter('[%(asctime)s] %(levelname)s in %(module)s: %(message)s')
monthly_handler.setFormatter(formatter)
if app.logger.hasHandlers():
    app.logger.handlers.clear()
app.logger.addHandler(monthly_handler)
app.logger.setLevel(logging.INFO)

# filepath: app.py
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.join(basedir, 'var', 'app-instance', 'bills.db')}"
app.config['SECRET_KEY'] = secrets.token_hex(16)
db = SQLAlchemy(app)
migrate = Migrate(app, db)

# Models
class PaymentHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    amount_due = db.Column(db.Float, nullable=False)
    amount_paid = db.Column(db.Float, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    date_paid = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    bill_id = db.Column(db.Integer, db.ForeignKey('bill.id', name="fk_payment_history_bill_id"), nullable=False)
    bill = db.relationship('Bill', backref=db.backref('payment_history', lazy=True))

class Bill(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    due_date = db.Column(db.String(100), nullable=False)
    is_automatic = db.Column(db.Boolean, default=False)
    category = db.Column(db.String(50), nullable=True, default='Miscellaneous')
    frequency = db.Column(db.String(20), nullable=True, default='monthly')  # e.g., 'monthly', 'weekly', 'annually'
    interest_rate = db.Column(db.Float, nullable=True)  # For credit cards and loans
    balance = db.Column(db.Float, nullable=True)  # Current balance for credit cards and loans
    created_at = db.Column(db.DateTime, nullable=True, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True, default=datetime.utcnow, onupdate=func.now())

class Income(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    frequency = db.Column(db.String(100), nullable=False)

def __repr__(self):
    return f'<PaymentHistory {self.name}>'

with app.app_context():
    db.create_all()

# Routes
@app.route('/')
def index():
    return redirect(url_for('current_bills'))

@app.route('/send_payperiod')
def create_payperiod():
    # insert a javascript popup
    biweekly_email_job(app)

@app.route('/bills')
def bills():
    bills = Bill.query.all()
    incomes = Income.query.all()
    bills_list = list(bills)
    bills_list.sort(key=lambda x: datetime.strptime(x.due_date, '%d'))
    return render_template('list_bills.html', bills=bills_list, incomes=incomes)

@app.route('/income')
def income():
    bills = Bill.query.all()
    incomes = Income.query.all()
    bills_list = list(bills)
    bills_list.sort(key=lambda x: datetime.strptime(x.due_date, '%d'))
    return render_template('list_income.html', bills=bills_list, incomes=incomes)

@app.template_filter('strftime')
def _jinja2_filter_datetime(date_string, format='%b %d, %Y'):
    date_object = datetime.strptime(date_string, '%m/%d/%Y')  # Adjust the input format as needed
    return date_object.strftime(format)

@app.template_filter('to_datetime')
def to_datetime(date_string, date_format='%m/%d/%Y'):
    """
    Converts a date string into a datetime object.
    :param date_string: The date string to convert.
    :param date_format: The format of the date string (default: '%m-%d-%Y').
    :return: A datetime object.
    """
    return datetime.strptime(date_string, date_format).date()

@app.route('/current-bills', methods=['GET', 'POST'])
def current_bills():
    app.logger.info(f"in current_bills")
    today = datetime.now().date()
    
    next_period = request.args.get('next_period')
    previous_period = request.args.get('previous_period')

    if next_period:
        new_period = next_period.split('-')
        pay_period_start = datetime(int(new_period[0]), int(new_period[1]), int(new_period[2])).date()
        pay_period_end = pay_period_start + timedelta(days=SETTINGS.pay_period_days)

    elif previous_period:
        new_period = previous_period.split('-')
        pay_period_start = datetime(int(new_period[0]), int(new_period[1]), int(new_period[2])).date()
        pay_period_end = pay_period_start + timedelta(days=SETTINGS.pay_period_days)
    else:
        pay_period_start, pay_period_end= get_payday_start(SETTINGS.main_payday_name, SETTINGS.pay_period_days)

    app.logger.info(f"Start: {pay_period_start}, End: {pay_period_end}")

    (bills, income_schedule, total_income) = collect_bills_and_income(pay_period_start, pay_period_end)

    app.logger.info(f"Total Income: {total_income}")

    # If the user hits the 'Pay' button then the PaymentHistory table is updated
    if request.method == 'POST':
        bill_id = request.form.get('bill_id')
        full_due_date = datetime.strptime(request.form.get('full_due_date'), '%m/%d/%Y').date()
        app.logger.info(f"Bill: {bill_id} Due Date: {full_due_date}")
        amount_paid = float(request.form.get('amount_paid'))
        bill = Bill.query.get(bill_id)

        if bill:
            payment = PaymentHistory(
                name=bill.name,
                amount_due=bill.amount,
                amount_paid=amount_paid,
                due_date=full_due_date,
                date_paid=today,
                bill_id=bill_id
            )
            db.session.add(payment)
            db.session.commit()

        return redirect(url_for('current_bills'))

    return render_template('current_bills.html', bills=bills, incomes=income_schedule, total=total_income, start_period=pay_period_start, end_period=pay_period_end, timedelta=timedelta, date_string=to_datetime)


@app.route('/payment-history')
def payment_history():
    bills = Bill.query.all()
    incomes = Income.query.all()
    bills_list = list(bills)
    bills_list.sort(key=lambda x: datetime.strptime(x.due_date, '%d'))
    payments = PaymentHistory.query.order_by(PaymentHistory.date_paid.desc()).all()
    return render_template('payment_history.html', payments=payments, bills=bills_list, incomes=incomes)

@app.route('/add_bill', methods=['GET', 'POST'])
def add_bill():
    if request.method == 'POST':
        name = request.form['name']
        amount = float(request.form['amount'])
        due_date = request.form['due_date']
        category = request.form['category']
        frequency = request.form['frequency']
        interest_rate = request.form['interest_rate']
        balance = request.form['balance']
        is_automatic = 'is_automatic' in request.form
        new_bill = Bill(name=name, amount=amount, due_date=due_date, category=category, frequency=frequency, interest_rate=interest_rate, balance=balance, is_automatic=is_automatic)
        db.session.add(new_bill)
        db.session.commit()
        flash('Bill added successfully', 'success')
        return redirect(url_for('bills'))
    return render_template('add_bill.html')

@app.route('/add_income', methods=['GET', 'POST'])
def add_income():
    if request.method == 'POST':
        name = request.form.get('name', '')
        amount_str = request.form.get('amount', '')
        frequency = request.form.get('frequency_type', '')
        amount = "{:.2f}".format(float(amount_str))
        app.logger.info(f"At ADD INCOME frequency is {frequency}")

        # Frequency-specific validations
        if frequency == "one_time_deposit":
            one_time_date = request.form.get('one_time_date')
            frequency_value = f"One time deposit on {one_time_date}"

        elif frequency == "every_nth_day":
            every_nth_day_frequency_value = request.form.get('every_nth_day_frequency_value')
            nth_day = format_every_nth_day(every_nth_day_frequency_value)
            frequency_value = f"Every {nth_day} of the month"

        elif frequency == "every_n_weeks":
            every_n_weeks_frequency_value = request.form.get('every_n_weeks_frequency_value')
            every_n_weeks_start_date = request.form.get('every_n_weeks_start_date')
            frequency_value = f"Every {every_n_weeks_frequency_value} weeks starting {every_n_weeks_start_date}"

        elif frequency == "every_nth_weekday":
            every_nth_weekday_frequency_value = request.form.get('every_nth_weekday_frequency_value')
            selected_weekday = request.form.get('select_weekday')
            app.logger.info(f"selected_weekday is {selected_weekday}")
            nth_day = format_every_nth_day(every_nth_weekday_frequency_value)
            frequency_value = f"Every {nth_day} {selected_weekday} of the month"


        new_income = Income(name=name, amount=amount, frequency=frequency_value)
        db.session.add(new_income)
        db.session.commit()
        app.logger.info(f"frequency is set to {frequency_value}")
        flash('Income added successfully', 'success')
        return redirect(url_for('income'))
    else:
        return render_template('add_income.html')

@app.route('/edit_income/<int:id>', methods=['GET', 'POST'])
def edit_income(id):
    income = Income.query.get_or_404(id)
    start_date = datetime(2000, 1, 1).date()
    end_date = datetime(2100, 1, 1).date()
    app.logger.info(f"in edit_incomes")
    app.logger.info(f"income.frequency is {income.frequency}")
    payday, frequency = parse_income_pattern(income.frequency, start_date, end_date)
    #income.pay_frequency = frequency

    if request.method == 'POST':
        app.logger.info("Updating income with ID: %s", id)
        app.logger.info("Form data: %s", request.form)
        income.name = request.form['name']
        income.amount = float(request.form['amount'])
        frequency = request.form['frequency_type']
        app.logger.info(f"At EDIT INCOME frequency_type is {frequency}")

        # Frequency-specific validations
        if frequency == "one_time_deposit":
            one_time_date = request.form.get('one_time_date')
            frequency_value = f"One time deposit on {one_time_date}"

        elif frequency == "every_nth_day":
            every_nth_day_frequency_value = request.form.get('every_nth_day_frequency_value')
            nth_day = format_every_nth_day(every_nth_day_frequency_value)
            frequency_value = f"Every {every_nth_day_frequency_value}{nth_day} of the month"

        elif frequency == "every_n_weeks":
            every_n_weeks_frequency_value = request.form.get('every_n_weeks_frequency_value')
            every_n_weeks_start_date = request.form.get('every_n_weeks_start_date')
            frequency_value = f"Every {every_n_weeks_frequency_value} weeks starting {every_n_weeks_start_date}"

        elif frequency == "every_nth_weekday":
            every_nth_weekday_frequency_value = request.form.get('every_nth_weekday_frequency_value')
            selected_weekday = request.form.get('select_weekday')
            nth_day = format_every_nth_day(every_nth_weekday_frequency_value)
            frequency_value = f"Every {every_nth_weekday_frequency_value}{nth_day} {selected_weekday} of the month"

        income.frequency = frequency_value

        db.session.commit()
        flash('Income updated successfully', 'success')
        return redirect(url_for('index'))
    return render_template('edit_income.html', income=income, frequency=frequency, start_date=start_date, end_date=end_date)

@app.route('/edit_bill/<int:id>', methods=['GET', 'POST'])
def edit_bill(id):
    bill = Bill.query.get_or_404(id)
    app.logger.info(f"in edit_bill, bill is {bill.name} Due: {bill.due_date}")
    app.logger.info(f"Frequency is {bill.frequency}")
    if request.method == 'POST':
        app.logger.info("Updating bill with ID: %s", id)
        app.logger.info("Form data: %s", request.form)

        # Update fields
        bill.name = request.form['name']
        bill.amount = "{:.2f}".format(float(request.form['amount']))
        bill.due_date = request.form['due_date']
        bill.is_automatic = 'is_automatic' in request.form
        bill.category = request.form['category']
        bill.frequency = request.form['frequency']
        bill.interest_rate = request.form['interest_rate']
        bill.balance = request.form['balance']
        db.session.commit()
        flash('Bill updated successfully', 'success')
        return redirect(url_for('bills'))
    else:
        bill_due_date = int(bill.due_date)
    return render_template('edit_bill.html', bill=bill, bill_due_date=bill_due_date)

@app.route('/delete_bill/<int:id>', methods=['GET', 'POST'])
def delete_bill(id):
    bill = Bill.query.get_or_404(id)

    confirmation_message = None
    action_result = None

    if request.method == 'POST':
        if request.form.get('confirm_delete') == 'true':
            # Handle confirmed deletion:
            db.session.delete(bill)
            db.session.commit()
            return redirect(url_for('bills'))

        elif request.form.get('confirm_delete') == 'false':
            #Handle cancelled deletion
            return redirect(url_for('bills'))

    return render_template('list_bills.html', bill=bill, confirmation_message=confirmation_message, action_result = action_result)

@app.route('/delete_income/<int:id>')
def delete_income(id):
    income = Income.query.get_or_404(id)
    db.session.delete(income)
    db.session.commit()
    flash('Income deleted successfully', 'success')
    return redirect(url_for('index'))

@app.route('/report')
def report():
    return render_template('report.html')

@app.route('/report/data')
def report_data():
    timeframe = request.args.get('timeframe', 'current_month')  # Default to current month

    # Get Start and End dates for a given timeframe
    today = date.today()

    if timeframe == 'current_month':
        start_date = date(today.year, today.month, 1)
        end_date = date(today.year, today.month, monthrange(today.year, today.month)[1])
    elif timeframe == 'last_6_months':
        start_date = today - relativedelta(months=5) # Use relativedelta to go back 5 months
        start_date = date(start_date.year, start_date.month, 1)
        end_date = date(today.year, today.month, monthrange(today.year, today.month)[1])
    elif timeframe == 'last_12_months':
          start_date = today - relativedelta(months=11)  # use relativedelta to go back 11 months
          start_date = date(start_date.year, start_date.month, 1)
          end_date = date(today.year, today.month, monthrange(today.year, today.month)[1])
    elif timeframe == 'year':
        start_date = date(today.year, 1, 1)
        end_date = date(today.year, 12, 31)
    else:
        return jsonify(error="Invalid timeframe selected"), 400

    #Get all the bills and income, and return a JSON object for the graph.
    bills = Bill.query.all()
    payment_history = PaymentHistory.query.filter(
        and_(
            PaymentHistory.date_paid >= start_date,
            PaymentHistory.date_paid <= end_date
        )
    ).all()

    income_list = Income.query.all()
    income_total = {}
    bill_total = {}
    
    # Create a dictionary to be able to quickly find any matching payments
    payment_dict = {}
    for payment in payment_history:
        payment_dict.setdefault(payment.bill_id, []).append(payment)

    for bill in bills:
        if bill.id in payment_dict:
            payments_for_bill = payment_dict[bill.id]
            # This logic assumes you have one payment per bill. if you have multiple,
            # adjust according to your logic. For multiple you may want to take sum of each payment for each bill.
            if payments_for_bill:
                payment = payments_for_bill[0]  # Consider only the first payment
                bill.amount_paid = payment.amount_paid
                bill.date_paid = payment.date_paid.strftime('%m/%d/%Y') # Formatting the date if available
            else:
                bill.amount_paid = None
                bill.date_paid = None
        else:
            bill.amount_paid = None
            bill.date_paid = None

         #Determine the full due date based on pay_period_start.
        #Use pay_period_start year and month, then set the day of the month
        due_day = int(bill.due_date)
        full_due_date = datetime(start_date.year, start_date.month, due_day).date()

        # check if the full due date is before the start
        # if so we know that the bill is in the *next* month
        if full_due_date < start_date:
              #get next month
              if (start_date.month == 12):
                  next_month = 1
                  next_year = start_date.year +1
              else:
                  next_month = start_date.month + 1
                  next_year = start_date.year

              full_due_date = date(next_year, next_month, due_day)


        # Check if the bill is within the current pay period
        if full_due_date >= start_date and full_due_date <= end_date:

            bill_total[bill.category] = bill_total.get(bill.category, 0) + bill.amount

    for income in income_list:
        # Get all the income entries for the selected period
          payday = parse_income_pattern(income.frequency, start_date, end_date)
          if payday >= start_date and payday <= end_date:
             income_total[income.name] = income_total.get(income.name,0) + income.amount

    # Prepare the data for the chart.
    labels = list(set(income_total.keys()).union(bill_total.keys()))
    income_data = [income_total.get(label, 0) for label in labels]
    bill_data = [bill_total.get(label,0) for label in labels]


    # Return the data as a JSON object
    return jsonify(labels = labels, income_data = income_data, bill_data = bill_data)

@app.template_filter('debug')
def debug_filter(s):
    return f"<pre>{s}</pre>"

# Email sending function
def send_email(subject, body):
    sender_email = "pfhendr@gmail.com"
    receiver_email = "pfhendr@gmail.com"
    password = "rybakwiawjmqnxkw"

    message = MIMEMultipart('alternative')
    message["From"] = sender_email
    message["To"] = receiver_email
    message["Subject"] = subject

    html_part = MIMEText(body, 'html')
    message.attach(html_part)


    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender_email, password)
        server.sendmail(sender_email, receiver_email, message.as_string())

# Daily email job with automatic payment processing
def daily_email_job(app):
    with app.app_context():
      total = 0
      app.logger.info(f"called: daily_email_job()")  
      email_body = ""
      today = datetime.now().date()
      (due_bills, upcoming_incomes, balance) = collect_bills_and_income(today, today)
      app.logger.info(f"In Daily Email Job")

      if due_bills:
        subject = f"Bills due today ({today})"
        for bill in due_bills:
            # Process automatic payments
            if bill.is_automatic:
                # Check if payment was already made today for this bill
                existing_payment = PaymentHistory.query.filter(
                    and_(
                        PaymentHistory.bill_id == bill.id,
                        PaymentHistory.due_date == today,
                        cast(PaymentHistory.date_paid, Date) == today
                    )
                ).first()
                
                # Only create a payment record if one doesn't already exist
                if not existing_payment:
                    app.logger.info(f"Processing automatic payment for {bill.name}")
                    payment = PaymentHistory(
                        name=bill.name,
                        amount_due=bill.amount,
                        amount_paid=bill.amount,
                        due_date=today,
                        date_paid=today,
                        bill_id=bill.id
                    )
                    db.session.add(payment)
                    db.session.commit()
                    app.logger.info(f"Automatic payment recorded for {bill.name}")
            
            # Build email notification for all due bills
            email_body +=f"<TR><TD>{bill.name}</TD><TD>${bill.amount:.2f}</TD><TD>"
            if bill.is_automatic:
                email_body += "Yes"
            else:
                email_body += ""
            email_body += "</TD></TR>\n"

            total += 1

        if total:
            header = '''
    	    <style type="text/css">
	        table {border-collapse:collapse; background-color: #FFFFE0;}
	        table,td,th {border:1px solid black, padding: 5px}
	        table,td,th {td font-family: "Arial Black", sans-serif;}
	        table,td font-size: 95%;
	        </style><BODY>
            '''
            table = '''<table border=1 width="90%" cellspacing='0'>
	        <TR><TH>Bill</TH><TH>Amount</TH><TH>Automatic</TH>
            '''
            body = header
            body += table
            body += email_body
            body += "</table>"
            body += "</table></BODY></html>"

            send_email(subject, body)
            app.logger.info(f"Email sent with subject: {subject}")

# Bi-weekly email job
def biweekly_email_job(app):
    with app.app_context():
      app.logger.info(f"Called biweekly_email_job")
      start_date = datetime(2023, 2, 3)  # First payday
      today = datetime.now()
      days_since_start = (today - start_date).days
      app.logger.info(f"biweekly_email_job(): start_date{start_date} today: {today} days_since_start: {days_since_start}")

      header = '''
    	<style type="text/css">
	    table {border-collapse:collapse; background-color: #FFFFE0;}
	    table,td,th {border:1px solid black; padding: 5px}
	    table,td,th {td font-family: "Arial Black", sans-serif;}
	    table,td font-size: 95%;
	    </style><BODY>
      '''
      table = '''<table border=1 width="90%" cellspacing='0'>
	   <TR><TH>Date</TH><TH>Deposit</TH><TH>Bill</TH><TH>Amount</TH><TH>Balance</TH>
      '''

      if days_since_start:
        total_income = 0
        total_bills = 0
        total_balance = 0

        pay_period_start, pay_period_end = get_payday_start(SETTINGS.main_payday_name, SETTINGS.pay_period_days)
        app.logger.info(f"Start: {pay_period_start} End: {pay_period_end}")
        (upcoming_bills, upcoming_incomes, balance) = collect_bills_and_income(pay_period_start, pay_period_end)

        app.logger.info(f"Upcoming bills: {upcoming_bills}")
        app.logger.info(f"Upcoming incomes: {upcoming_incomes}")

        subject = "Bi-weekly Financial Summary"
        body = ""

        body += header
        body += table

        for day_offset in range((pay_period_end - pay_period_start).days + 1):
            date_str = (pay_period_start + timedelta(days=day_offset)).strftime('%m/%d/%Y')
            current_date = datetime.strptime(date_str, '%m/%d/%Y').date()
            for income_date in upcoming_incomes:
                if income_date == current_date:
                    for income_entry in upcoming_incomes[income_date]:  # Iterate over the list of incomes
                        app.logger.debug(f"Processing income entry: {income_entry}")
                        total_income += income_entry['amount']
                        total_balance += income_entry['amount']
                        body += f"<TR><TD>{current_date}</TD><TD>{income_entry['name']}</TD><TD></TD><TD>${income_entry['amount']:.2f}</TD><TD>${total_balance:.2f}</TD></TR>\n"

            for bill in upcoming_bills:
                bill_due = datetime.strptime(bill.formatted_due_date, '%m/%d/%Y').date()
                if bill_due == current_date:
                    total_balance -= bill.amount
                    total_bills += bill.amount
                    app.logger.debug(f"Processing bill: {bill.name} due on {bill_due} for amount {bill.amount} total bills: {total_bills} total balance: {total_balance}")
                    body +=f"<TR><TD>{current_date}</TD><TD></TD><TD>{bill.name}</TD><TD>${bill.amount:.2f}</TD><TD>${total_balance:.2f}</TD></TR>\n"

        body += f"<tr><TH colspan='3'>Total Bills</th><th>${total_bills:.2f}</th><th>${total_balance:.2f}</th></tr>"
        body += "</table>"
        body += "</table></BODY></html>"

        send_email(subject, body)

@app.route('/send_biweekly_report', methods=['POST'])
def send_biweekly_report():
    try:
        biweekly_email_job(app)
        return jsonify({'success': True, 'message': 'Biweekly email sent.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

def parse_income_pattern(frequency, start_date, end_date):
    """
    Parse income pattern to determine when new income comes in.

    Args:
        pattern (str): Income pattern (e.g. 'Every 2 weeks', 'Every 2nd Wednesday', 'Every 15th of the month')
        pay_period_start (datetime.date): Start date of the pay period
        pay_period_end (datetime.date): End date of the pay period

    Returns:
        list: List of dates when new income comes in
    """
    app.logger.info(f"parse_income_pattern: {frequency}")
    pay_freqency = ""

    # Return the oldest date in case where nothing matches
    payday = datetime(1970, 1, 1).date()

    patterns = {
        "every_day_of_month": re.compile(r"Every (\d+)(?:st|nd|rd|th) day of the month", re.IGNORECASE),
        "every_nth_day": re.compile(r"Every (\d+)(?:st|nd|rd|th) of the month", re.IGNORECASE),
        "every_weekday": re.compile(r"Every (\d+)(?:st|nd|rd|th) (monday|tuesday|wednesday|thursday|friday|saturday|sunday)", re.IGNORECASE),
        "one_time": re.compile(r"One time deposit on (\d{4}-\d{2}-\d{2})", re.IGNORECASE),
        "every_weeks": re.compile(r"Every (\d+) weeks starting (\d{4}-\d{2}-\d{2})", re.IGNORECASE)
    }

    for key, pattern in patterns.items():
        app.logger.info(f"trying {key}")
        app.logger.info(f"pattern {pattern}")
        match = pattern.match(frequency)
        if match:
            app.logger.info(f"matched {key}")
            if key == "every_day_of_month" or key == "every_nth_day":
                app.logger.info(f"found every_day_of_month")
                pay_frequency = "every_day_of_month"
                day_of_month = int(match.group(1))
                for dt in [start_date,end_date]:
                    try:
                        test_date = datetime(dt.year, dt.month, day_of_month).date()
                        if start_date <= test_date <= end_date:
                            payday = test_date
                    except ValueError:
                        #Handle if day of month does not exist for the given month
                        pass

            elif key == "every_weekday":
                app.logger.info(f"found every_weekday")
                pay_frequency = "every_weekday"
                day_index = int(match.group(1))
                weekday_str = match.group(2).lower()
                weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
                try:
                    weekday = weekdays.index(weekday_str)
                except ValueError:
                    app.logger.info(f"Invalid weekday : {weekday_str}")
                    return payday


                for dt in [start_date,end_date]:
                     first_day_of_month = datetime(dt.year, dt.month, 1).date()
                     #Calculate the first occurrence of the weekday in month
                     first_weekday_of_month = first_day_of_month + timedelta((weekday - first_day_of_month.weekday() + 7) % 7)

                     #Calculate the Nth day of the weekday in month
                     test_date = first_weekday_of_month + timedelta((day_index-1)*7)

                     if start_date <= test_date <= end_date:
                         payday = test_date


            elif key == "one_time":
                app.logger.info(f"found one_time")
                pay_frequency = "one_time"
                date_str = match.group(1)
                date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if start_date <= date <= end_date:
                    payday = date

            elif key == "every_weeks":
                app.logger.info(f"found every_weeks")
                pay_frequency = "every_weeks"
                weeks = int(match.group(1))
                current_date_str = match.group(2)
                current_date = datetime.strptime(current_date_str, "%Y-%m-%d").date()
                while current_date <= end_date:
                     if start_date <= current_date <= end_date:
                            payday = current_date
                     current_date += timedelta(weeks=weeks)

    app.logger.info(f"Frequency: {pay_frequency}")
    return payday, pay_frequency

def get_last_day_of_month(year, month):
    _, last_day = calendar.monthrange(year, month)
    return date(year, month, last_day)

def get_payday_start(pattern, pay_period_days):
    today = datetime.now().date()
    start_date = today
    start_date -= timedelta(days=31)
    app.logger.info(f"get_payday_start: start_date: {start_date} today: {today}")
    try:
        incomes = Income.query.all()
        app.logger.info(f"total incomes: {Income.query.count()}")
    except Exception as e:
        app.logger.info(f"Error querying incomes: {e}")
        incomes = []

    payday = date(1, 1, 1)
    # Add debug info
    app.logger.info(f"get_payday_start: pattern: {pattern} pay_period_days: {pay_period_days} today: {today}")
    app.logger.info(f"Incomes: {incomes}")

    for income in incomes:
        app.logger.info(f"Checking income for {pattern} - Found {income.name}")
        if re.search(pattern, income.name, re.IGNORECASE):
          payday, key = parse_income_pattern(income.frequency, start_date, today)
          app.logger.info(f"Payday: {payday} key: {key}")

    end_period = payday
    end_period += timedelta(days=pay_period_days)
    return payday, end_period

def collect_bills_and_income(pay_period_start, pay_period_end):
    current_bills = []
    payment_dict = {}
    app.logger.info(f"Start Pay Period {pay_period_start} End Pay Period {pay_period_end}")

    # Extract the days of the month for the pay period
    start_day = pay_period_start.day
    end_day = pay_period_end.day
    start_month = pay_period_start.month
    end_month = pay_period_end.month
    start_year = pay_period_start.year
    end_year = pay_period_end.year

    # Get the payment history for the current pay period
    start_date = datetime(start_year, start_month, start_day).date()
    end_date = datetime(end_year, end_month, end_day).date()

    total_income = 0

    incomes = Income.query.all()
    income_schedule = {}

    # Total up the amount of income during this pay period
    for income in incomes:
        payday, key = parse_income_pattern(income.frequency, pay_period_start, pay_period_end)

        if payday not in income_schedule:
            income_schedule[payday] = []

        if payday >= pay_period_start and payday <= pay_period_end:
            income_schedule[payday].append({'amount': income.amount, 'name': income.name})
            total_income += income.amount
            app.logger.debug(f"Income: {income.name} ({income.amount}) is in pay period, total income: {total_income}")
        else:
            app.logger.debug(f"Income: {income.name} not in pay period")


    bills = Bill.query.all()
    payment_history = PaymentHistory.query.filter(
        and_(
            PaymentHistory.date_paid >= start_date,
            PaymentHistory.date_paid <= end_date
        )
    ).all()

    # create a temporary dictionary to be able to quickly find any matching payments
    for payment in payment_history:
        payment_dict.setdefault(payment.bill_id, []).append(payment)  # Use bill_id as the key for the payment_dict

    for bill in bills:
    
        if bill.id in payment_dict:
            payments_for_bill = payment_dict[bill.id]

            # This logic assumes you have one payment per bill. if you have multiple, 
            # adjust according to your logic. For multiple you may want to take sum of each payment for each bill.
            if payments_for_bill:
                payment = payments_for_bill[0]  # Consider only the first payment
                bill.amount_paid = payment.amount_paid
                bill.date_paid = payment.date_paid.strftime('%m/%d/%Y') # Formatting the date if available
                # Print debug information
                #app.logger.debug(f"Bill: {bill.name} payment amount {payment.amount_paid} date {payment.date_paid}")
            else:
                bill.amount_paid = None
                bill.date_paid = None
        else:
            bill.amount_paid = None
            bill.date_paid = None

        # Convert the dates to a date object for easy comparisons
        due_date = datetime.strptime(bill.due_date, '%d').date()
        start_bill_day = datetime(start_year, start_month, start_day).date()
        end_bill_day = datetime(end_year, end_month, end_day).date()
        full_due_date = datetime(start_year, start_month, due_date.day).date()

        # check if the full due date is before the start
        # if so we know that the bill is in the *next* month
        if full_due_date < pay_period_start:
              #get next month
              if (start_month == 12):
                  next_month = 1
                  next_year = start_year +1
              else:
                  next_month = start_month + 1
                  next_year = start_year

              full_due_date = datetime(next_year, next_month, due_date.day).date()
            
        if full_due_date >= start_bill_day and full_due_date <= end_bill_day:
            bill.formatted_due_date = full_due_date.strftime('%m/%d/%Y')
            current_bills.append(bill)
    
    return current_bills, income_schedule, total_income

def format_every_nth_day(n):
    app.logger.info(f"format_every_nth_day: {n}")  
    n = int(str(n))
    if n <= 0:
        return "Invalid input: number must be positive"
    
    # Get the appropriate ordinal suffix
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    
    return f"{n}{suffix}"

if __name__ == '__main__':

    # Set up scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(daily_email_job, 'cron', hour=7, minute=00, args=[app])
    scheduler.add_job(biweekly_email_job, IntervalTrigger(weeks=2, start_date='2025-04-25 07:00:00'), args=[app])

    if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        app.logger.info(f"Scheduler started")
        scheduler.start()

    #app.run(host='0.0.0.0', port=80, debug=False)
    app.run(host='0.0.0.0', port=8080, debug=True)

