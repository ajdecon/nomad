import datetime
from flask import (
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required
from flask_mail import Message
from geoalchemy2 import func
from geoalchemy2.shape import to_shape, from_shape
from shapely.geometry import mapping, Point
from . import pool_bp
from .forms import (
    CancelCarpoolDriverForm,
    DriverForm,
    RiderForm,
)
from ..models import Carpool, Destination, RideRequest
from .. import db, mail


@pool_bp.route('/', methods=['GET', 'POST'])
def index():
    return render_template('index.html')


@pool_bp.route('/carpools/find')
def find():
    return render_template('carpools/find.html')


@pool_bp.route('/carpools/starts.geojson')
def start_geojson():
    pools = Carpool.query

    if request.args.get('ignore_prior') != 'false':
        pools = pools.filter(Carpool.leave_time >= datetime.datetime.utcnow())

    min_leave_date = request.args.get('min_leave_date')
    if min_leave_date:
        try:
            min_leave_date = datetime.datetime.strptime(
                min_leave_date, '%m/%d/%Y')
            pools = pools.filter(
                func.date(Carpool.leave_time) == min_leave_date)
        except ValueError:
            abort(400, "Invalid date format for min_leave_date")

    near_lat = request.args.get('near.lat')
    near_lon = request.args.get('near.lon')
    if near_lat and near_lon:
        try:
            near_lat = float(near_lat)
            near_lon = float(near_lon)
        except ValueError:
            abort(400, "Invalid lat/lon format")
        center = from_shape(Point(near_lon, near_lat), srid=4326)
        pools = pools.order_by(func.ST_Distance(Carpool.from_point, center))

    features = []
    for pool in pools:
        if pool.from_point is None:
            continue

        features.append({
            'type': 'Feature',
            'geometry': mapping(to_shape(pool.from_point)),
            'id': url_for('carpool.details', carpool_id=pool.id),
            'properties': {
                'from_place': pool.from_place,
                'to_place': pool.to_place,
                'seats_available': pool.seats_available,
                'leave_time': pool.leave_time.isoformat(),
                'return_time': pool.return_time.isoformat(),
                'driver_gender': pool.driver.gender,
            },
        })

    feature_collection = {
        'type': 'FeatureCollection',
        'features': features
    }

    return jsonify(feature_collection)


@pool_bp.route('/carpools/mine', methods=['GET', 'POST'])
@login_required
def mine():
    carpools = current_user.get_driving_carpools()

    return render_template('carpools/mine.html', carpools=carpools)


@pool_bp.route('/carpools/new', methods=['GET', 'POST'])
@login_required
def new():
    if not current_user.gender:
        flash("Please specify your gender before creating a carpool")
        session['next'] = url_for('carpool.new')
        return redirect(url_for('auth.profile'))

    driver_form = DriverForm()

    visible_destinations = Destination.find_all_visible().all()

    driver_form.going_to_list.choices = []
    driver_form.going_to_list.choices.append((-1, "Select a Destination"))
    driver_form.going_to_list.choices.extend([
        (r.id, r.name) for r in visible_destinations
    ])
    driver_form.going_to_list.choices.append((-2, "Other..."))

    if driver_form.validate_on_submit():
        c = Carpool(
            from_place=driver_form.leaving_from.data,
            from_point='SRID=4326;POINT({} {})'.format(
                driver_form.leaving_from_lon.data,
                driver_form.leaving_from_lat.data),
            to_place=driver_form.going_to_text.data,
            to_point='SRID=4326;POINT({} {})'.format(
                driver_form.going_to_lon.data,
                driver_form.going_to_lat.data),
            leave_time=driver_form.depart_time.data,
            return_time=driver_form.return_time.data,
            max_riders=driver_form.car_size.data,
            driver_id=current_user.id,
        )
        db.session.add(c)
        db.session.commit()

        flash("Thanks for adding your carpool!", 'success')

        return redirect(url_for('carpool.details', carpool_id=c.id))

    return render_template(
        'carpools/add_driver.html',
        form=driver_form,
        destinations=visible_destinations,
    )


@pool_bp.route('/carpools/<int:carpool_id>', methods=['GET', 'POST'])
def details(carpool_id):
    carpool = Carpool.query.get_or_404(carpool_id)

    return render_template('carpools/show.html', pool=carpool)


@pool_bp.route('/carpools/<int:carpool_id>/newrider', methods=['GET', 'POST'])
@login_required
def new_rider(carpool_id):
    carpool = Carpool.query.get_or_404(carpool_id)

    if carpool.current_user_is_driver:
        flash("You can't request a ride on a carpool you're driving in")
        return redirect(url_for('carpool.details', carpool_id=carpool_id))

    if not current_user.gender:
        flash("Please specify your gender before creating a carpool request")
        session['next'] = url_for('carpool.new_rider', carpool_id=carpool_id)
        return redirect(url_for('auth.profile'))

    rider_form = RiderForm()
    if rider_form.validate_on_submit():
        if carpool.seats_available < 1:
            flash("There isn't enough space for you on "
                  "this ride. Try another one?", 'error')
            return redirect(url_for('carpool.details', carpool_id=carpool_id))

        if carpool.get_current_user_ride_request():
            flash("You've already requested a seat on "
                  "this ride. Try another one or cancel your "
                  "existing request.", 'error')
            return redirect(url_for('carpool.details', carpool_id=carpool_id))

        rr = RideRequest(
            carpool_id=carpool.id,
            person_id=current_user.id,
            status='requested',
        )
        db.session.add(rr)
        db.session.commit()

        flash("You've been added to the list for this carpool!", 'success')
        # TODO send email to driver

        return redirect(url_for('carpool.details', carpool_id=carpool_id))

    return render_template('carpools/add_rider.html', form=rider_form)


@pool_bp.route('/carpools/<int:carpool_id>/request/<int:request_id>/<action>',
               methods=['GET', 'POST'])
@login_required
def modify_ride_request(carpool_id, request_id, action):
    # carpool = Carpool.query.get_or_404(carpool_id)
    request = RideRequest.query.get_or_404(request_id)

    # Technically the carpool arg isn't required here,
    # but it makes the URL prettier so there.

    if request.carpool_id != carpool_id:
        return redirect(url_for('carpool.details', carpool_id=carpool_id))

    # TODO Check who can modify a ride request. Only:
    #      1) the driver modifying their carpool
    #      2) the rider modifying their request
    #      3) an admin?

    # TODO This big messy if block should be a state machine

    if request.status == 'requested':
        if action == 'approve':
            request.status = 'approved'
            db.session.add(request)
            db.session.commit()
            flash("You approved their ride request.")
            # TODO Send email notification to rider
        elif action == 'deny':
            request.status = 'denied'
            db.session.add(request)
            db.session.commit()
            flash("You denied their ride request.")
            # TODO Send email notification to rider
        elif action == 'cancel':
            db.session.delete(request)
            db.session.commit()
            flash("You cancelled your ride request.")

    elif request.status == 'denied':
        if action == 'approve':
            request.status = 'approved'
            db.session.add(request)
            db.session.commit()
            flash("You approved their ride request.")
            # TODO Send email notification to rider

    elif request.status == 'approved':
        if action == 'deny':
            request.status = 'denied'
            db.session.add(request)
            db.session.commit()
            flash("You denied their ride request.")
            # TODO Send email notification to rider

    else:
        flash("You can't do that to the ride request.", "error")

    return redirect(url_for('carpool.details', carpool_id=carpool_id))


@pool_bp.route('/carpools/<int:carpool_id>/cancel', methods=['GET', 'POST'])
@login_required
def cancel(carpool_id):
    carpool = Carpool.query.get_or_404(carpool_id)

    cancel_form = CancelCarpoolDriverForm()
    if cancel_form.validate_on_submit():
        if cancel_form.submit.data:
            _email_carpool_cancelled(carpool, cancel_form.reason.data)
            db.session.delete(carpool)
            db.session.commit()

            flash("Your carpool was cancelled", 'success')

            return redirect(url_for('carpool.index'))
        else:
            return redirect(url_for('carpool.details', carpool_id=carpool_id))

    return render_template('carpools/cancel.html', form=cancel_form)


def _email_carpool_cancelled(carpool, reason):
    driver = carpool.driver
    riders = carpool.riders_and_potential_riders
    if len(riders) == 0:
        return

    if not reason:
        reason = 'Reason not given!'

    subject = 'Carpool session on {} cancelled'.format(carpool.leave_time)

    for rider in riders:
        _send_email(
            'carpools/email/cancel_html.html',
            'carpools/email/cancel_html.html',
            rider.email,
            subject,
            driver=driver,
            rider=rider,
            carpool=carpool,
            reason=reason)


def _send_email(html_template, text_template, recipient, subject, **kwargs):
    body = render_template(text_template, **kwargs)
    html = render_template(html_template, **kwargs)
    msg = Message(
        recipients=[recipient], body=body, html=html, subject=subject)
    try:
        with mail.connect() as conn:
            conn.send(msg)
    except Exception as exception:
        current_app.logger.error(
            'Failed to send message to {} with subject {} and body {} Exception: {}'.
            format(recipient, subject, body, repr(exception)))
