from frasco import (Feature, action, request, signal, current_app, redirect, flash,\
                    url_for, cached_property, current_context, hook, Blueprint,\
                    lazy_translate)
import stripe
import datetime
import time


bp = Blueprint('stripe', __name__)
@bp.route('/stripe-webhook', methods=['POST'])
def webhook():
    if current_app.features.stripe.options['webhook_validate_event']:
        event = stripe.Event.retrieve(request.json['id'])
    else:
        event = stripe.convert_to_stripe_object(request.json,
            current_app.features.stripe.options['api_key'])
    signal_name = 'stripe_%s' % event.type.replace(".", "_")
    signal(signal_name).send(stripe, event=event)
    return 'ok'


class StripeFeature(Feature):
    name = "stripe"
    blueprints = [bp]
    defaults = {"default_plan": None,
                "auto_create_customer": True,
                "user_must_have_plan": False,
                "add_card_view": None,
                "no_payment_redirect_to": None,
                "no_payment_message": None,
                "subscription_past_due_message": lazy_translate(
                    u"We attempted to charge your credit card for your subscription but it failed."
                    "Please check your credit card details"),
                "only_one_card": False,
                "debug_trial_period": None,
                "send_invoice_email": True,
                "send_failed_invoice_email": True,
                "send_trial_will_end_email": True,
                "webhook_validate_event": False}

    def init_app(self, app):
        stripe.api_key = self.options['api_key']
        self.api = stripe
        stripe_creatable_attributes = ('Charge', 'Customer', 'Plan',
            'Coupon', 'Invoice', 'InvoiceItem', 'Transfer',
            'Recipient')
        stripe_attributes = stripe_creatable_attributes + \
            ('ApplicationFee', 'Account', 'Balance', 'Event', 'Token')
        for attr in stripe_attributes:
            setattr(self, attr, getattr(stripe, attr))
        for attr in stripe_creatable_attributes:
            app.actions.register(action("stripe_create_" + attr.lower())(getattr(stripe, attr).create))

        if app.features.exists("emails"):
            app.features.emails.add_templates_from_package(__name__)

        if app.features.exists('users'):
            model = app.features.models.ensure_model(app.features.users.model,
                stripe_customer_id=dict(type=str, index=True),
                stripe_subscription_id=dict(type=str, index=True),
                has_stripe_card=dict(type=bool, default=False, index=True),
                plan_name=dict(type=str, index=True),
                plan_status=dict(type=str, default='trialing', index=True),
                plan_last_charged_at=datetime.datetime,
                plan_last_charge_amount=float,
                plan_last_charge_successful=dict(type=bool, default=True, index=True),
                plan_next_charge_at=dict(type=datetime.datetime, index=True))
            model.stripe_customer = cached_property(self.find_customer_from_user)
            model.stripe_subscription = cached_property(self.find_user_current_subscription)
            model.stripe_card = cached_property(self.find_user_default_card)
            if self.options['auto_create_customer']:
                signal('user_signup').connect(lambda sender, user: self.create_customer(user), weak=False)
            signal('stripe_invoice_payment_succeeded').connect(self.on_invoice_payment_succeeded)
            signal('stripe_invoice_payment_failed').connect(self.on_invoice_payment_failed)
            signal('stripe_customer_subscription_created').connect(self.on_subscription_created)
            signal('stripe_customer_subscription_updated').connect(self.on_subscription_updated)
            signal('stripe_customer_subscription_deleted').connect(self.on_subscription_deleted)
            signal('stripe_customer_subscription_trial_will_end').connect(self.on_trial_will_end)

    def find_user_by_customer_id(self, cust_id):
        return current_app.features.users.query.filter(stripe_customer_id=cust_id).first()

    def find_user_by_subscription_id(self, subscription_id):
        return current_app.features.users.query.filter(stripe_subscription_id=subscription_id).first()

    def find_customer_from_user(self, user):
        try:
            return stripe.Customer.retrieve(user.stripe_customer_id)
        except stripe.error.InvalidRequestError:
            if self.options['auto_create_customer']:
                return self.create_customer(user)
            return

    def find_user_current_subscription(self, user):
        try:
            if not user.stripe_customer:
                return
            return user.stripe_customer.subscriptions\
                .retrieve(user.stripe_subscription_id)
        except stripe.error.InvalidRequestError:
            return

    def find_user_default_card(self, user):
        default_id = user.stripe_customer.default_card
        if default_id:
            return user.stripe_customer.cards.retrieve(default_id)

    @hook()
    def before_request(self):
        if request.endpoint in (self.options['add_card_view'], 'users.logout') or 'static' in request.endpoint:
            return
        if current_app.features.users.logged_in():
            user = current_app.features.users.current
            if not user.plan_name and self.options['user_must_have_plan'] and self.options['default_plan']:
                self.subscribe_user(self.options['default_plan'], user=user)
            if (not user.plan_name and self.options['user_must_have_plan']) or \
               (user.plan_name and ((user.plan_status not in ('trialing', 'active') and not user.has_stripe_card) or \
                                     user.plan_status in ('canceled', 'unpaid'))):
                if self.options['no_payment_message']:
                    flash(self.options['no_payment_message'], 'error')
                if self.options['no_payment_redirect_to']:
                    return redirect(self.options['no_payment_redirect_to'])
                if self.options['add_card_view']:
                    return redirect(url_for(self.options['add_card_view']))
            if user.plan_status == 'past_due' and \
              self.options['subscription_past_due_message']:
                flash(self.options['subscription_past_due_message'], 'error')

    @action('stripe_create_customer', default_option='user')
    def create_customer(self, user=None):
        if not user:
            user = current_app.features.users.current
        cust = stripe.Customer.create(email=user.email)
        user.stripe_customer_id = cust.id
        user.save()
        if self.options['default_plan']:
            self.subscribe_user(self.options['default_plan'], user=user)
        return cust

    @action('stripe_add_card', default_option='token')
    def add_user_card(self, user=None, token=None, **card_details):
        if not user:
            user = current_app.features.users.current
        if self.options['only_one_card'] and user.stripe_customer.default_card:
            user.stripe_customer.cards.retrieve(
                user.stripe_customer.default_card).delete()
        user.stripe_customer.cards.create(card=token or card_details)
        user.has_stripe_card = True
        user.save()
        if not user.plan_name and self.options['user_must_have_plan'] \
          and self.options['default_plan']:
            self.subscribe_user(self.options['default_plan'], user=user)

    @action('stripe_add_card_from_form')
    def add_user_card_from_form(self, user=None, form=None):
        if not user:
            user = current_app.features.users.current
        form = current_context.data.get('form')
        if form and "stripeToken" in form:
            self.add_user_card(user, form.stripeToken.data)
        elif "stripeToken" in request.form:
            self.add_user_card(user, request.form['stripeToken'])
        elif form:
            self.add_user_card(user,
                number=form.card_number.data,
                exp_month=form.card_exp_month.data,
                exp_year=form.card_exp_year.data,
                cvc=form.card_cvc.data,
                name=form.card_name.data)
        else:
            raise Exception("No form found to retrieve the stripeToken")

    @action('stripe_remove_card')
    def remove_card(self, card_id=None, user=None):
        if not user:
            user = current_app.features.users.current
        if not card_id:
            card_id = user.stripe_customer.default_card
        try:
            card = user.stripe_customer.cards.retrieve(card_id)
        except stripe.error.InvalidRequestError:
            return
        card.delete()
        if user.stripe_customer.cards.total_count == 1:
            # there was only one card
            user.has_stripe_card = False
            user.save()

    @action('stripe_subscribe_user', default_option='plan')
    def subscribe_user(self, plan=None, quantity=1, user=None, customer=None, **kwargs):
        if not plan:
            plan = self.options['default_plan']
        if not user:
            user = current_app.features.users.current
        if user.plan_name == plan:
            return
        params = dict(plan=plan, quantity=quantity)
        if self.options['debug_trial_period'] and current_app.debug:
            if self.options['debug_trial_period'] == 'now':
                params['trial_end'] = 'now'
            else:
                trial_end = datetime.datetime.now() + \
                    datetime.timedelta(days=self.options['debug_trial_period'])
                params['trial_end'] = int(time.mktime(trial_end.timetuple()))
        if not customer:
            customer = user.stripe_customer
        params.update(kwargs)
        subscription = customer.subscriptions.create(**params)
        self.update_subscription_details(user, subscription)
        return subscription

    def update_subscription_details(self, user, subscription):
        if subscription and subscription.status != 'canceled':
            user.stripe_subscription_id = subscription.id
            user.plan_name = subscription.plan.id
            user.plan_status = subscription.status
            if user.plan_status == 'trialing':
                user.plan_next_charge_at = datetime.datetime.fromtimestamp(subscription.trial_end)
            else:
                user.plan_next_charge_at = datetime.datetime.fromtimestamp(subscription.current_period_end)
        else:
            user.stripe_subscription_id = None
            user.plan_name = None
            user.plan_status = None
            user.plan_next_charge_at = None
        user.save()

    @action('stripe_update_user_subscription', default_option='quantity')
    def update_user_subscription(self, user=None, **kwargs):
        if not user:
            user = current_app.features.users.current
        subscription = user.stripe_subscription
        for k, v in kwargs.iteritems():
            setattr(subscription, k, v)
        subscription.save()
        if "plan" in kwargs:
            user.plan_name = kwargs["plan"]
            user.save()

    @action('stripe_cancel_user_subscription', default_option='user')
    def cancel_user_subscription(self, user=None):
        if not user:
            user = current_app.features.users.current
        subscription = user.stripe_subscription
        subscription.delete()
        self.update_subscription_details(user, None)

    def update_last_subscription_charge(self, user, invoice, successful=True):
        subscription = user.stripe_subscription
        user.plan_last_charged_at = datetime.datetime.fromtimestamp(invoice.date)
        user.plan_last_charge_amount = invoice.total / 100
        user.plan_last_charge_successful = successful
        if successful:
            user.plan_next_charge_at = datetime.datetime.fromtimestamp(subscription.current_period_end)
        else:
            user.plan_next_charge_at = datetime.datetime.fromtimestamp(invoice.next_payment_attempt)
        user.save()

    def on_invoice_payment_succeeded(self, sender, event):
        self._on_invoice_payment(event)

    def on_invoice_payment_failed(self, sender, event):
        self._on_invoice_payment(event, False)

    def _on_invoice_payment(self, event, successful=True):
        invoice = event.data.object
        if not invoice.customer:
            return
        user = self.find_user_by_customer_id(invoice.customer)
        if not user or invoice.total == 0:
            return
        if invoice.subscription:
            sub_user = None
            if user.stripe_subscription_id == invoice.subscription:
                sub_user = user
            else:
                sub_user = self.find_user_by_subscription_id(invoice.subscription)
            if sub_user:
                self.update_last_subscription_charge(sub_user, invoice, successful)
        if (successful and self.options['send_invoice_email']) or \
           (not successful and self.options['send_failed_invoice_email']):
            self.send_invoice_email(user.email, invoice, not successful)

    def on_subscription_created(self, sender, event):
        subscription = event.data.object
        user = self.find_user_by_customer_id(subscription.customer)
        if not user or user.stripe_subscription_id:
            return
        self.update_subscription_details(user, subscription)

    def on_subscription_updated(self, sender, event):
        subscription = event.data.object
        user = self.find_user_by_subscription_id(subscription.id)
        if user:
            self.update_subscription_details(user, subscription)

    def on_subscription_deleted(self, sender, event):
        subscription = event.data.object
        user = self.find_user_by_subscription_id(subscription.id)
        if user:
            self.update_subscription_details(user, None)

    def on_trial_will_end(self, sender, event):
        subscription = event.data.object
        user = self.find_user_by_subscription_id(subscription.id)
        if not user:
            return
        if self.options['send_trial_will_end_email'] and not user.has_stripe_card:
            current_app.features.emails.send(user.email,
                'stripe/trial_will_end.txt', user=user)

    def send_invoice_email(self, email, invoice, failed=False, **kwargs):
        items = []
        for line in invoice.lines.data:
            desc = line.description or ''
            if line.plan:
                desc = line.plan.statement_description
            items.append((desc, line.amount / 100))
        template = 'stripe/failed_invoice.html' if failed else 'stripe/invoice.html'
        current_app.features.emails.send(email, template,
            invoice_date=datetime.datetime.fromtimestamp(invoice.date),
            invoice_items=items,
            invoice_currency=invoice.currency.upper(),
            invoice_total=invoice.total / 100, **kwargs)