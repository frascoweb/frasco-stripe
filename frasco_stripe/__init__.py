from frasco import (Feature, action, request, signal, current_app, redirect, flash,\
                    url_for, cached_property, current_context, hook, Blueprint,\
                    lazy_translate, command)
from frasco.utils import unknown_value
from frasco_models import as_transaction, save_model
import stripe
import datetime
import time
import os
import json


bp = Blueprint('stripe', __name__)

@bp.route('/stripe-webhook', methods=['POST'])
def webhook():
    if current_app.features.stripe.options['webhook_validate_event']:
        event = stripe.Event.retrieve(request.json['id'])
    else:
        event = stripe.Event.construct_from(request.json,
            current_app.features.stripe.options['api_key'])
    signal_name = 'stripe_%s' % event.type.replace(".", "_")
    signal(signal_name).send(stripe, stripe_event=event)
    return 'ok'


class StripeFeature(Feature):
    name = "stripe"
    blueprints = [bp]
    defaults = {"default_plan": None,
                "auto_create_customer": True,
                "must_have_plan": False,
                "add_source_view": None,
                "no_payment_redirect_to": None,
                "no_payment_message": None,
                "subscription_past_due_message": lazy_translate(
                    u"We attempted to charge your credit card for your subscription but it failed."
                    "Please check your credit card details"),
                "debug_trial_period": None,
                "send_trial_will_end_email": True,
                "send_failed_invoice_email": True,
                "invoice_ref_kwargs": {},
                "webhook_validate_event": False,
                "model": None,
                "email_attribute": "email",
                "billing_fields": True,
                "reset_billing_fields": True,
                "default_subscription_tax_percent": None,
                "eu_vat_support": None,
                "eu_auto_vat_country": True,
                "eu_vat_use_address_country": False,
                "eu_auto_vat_rate": True}

    model_source_updated_signal = signal('stripe_model_source_updated')
    model_subscription_updated_signal = signal('stripe_model_subscription_updated')
    model_last_charge_updated_signal = signal('stripe_model_last_charge_updated')
    invoice_payment_signal = signal('stripe_invoice_payment')

    def init_app(self, app):
        stripe.api_key = self.options['api_key']
        self.api = stripe
        stripe_creatable_attributes = ('Charge', 'Customer', 'Plan', 'Coupon', 'Invoice',
            'InvoiceItem', 'Transfer', 'Recipient')
        stripe_attributes = stripe_creatable_attributes + \
            ('ApplicationFee', 'Account', 'Balance', 'Event', 'Token')
        for attr in stripe_attributes:
            setattr(self, attr, getattr(stripe, attr))
            app.actions.register(action("stripe_retrieve_" + attr.lower())(getattr(stripe, attr).retrieve))
        for attr in stripe_creatable_attributes:
            app.actions.register(action("stripe_create_" + attr.lower())(getattr(stripe, attr).create))

        if app.features.exists("emails"):
            app.features.emails.add_templates_from_package(__name__)

        if app.features.exists('assets'):
            app.assets.register({'stripejs': ['https://js.stripe.com/v2/#.js']})
            if 'publishable_key' in self.options:
                app.config['EXPORTED_JS_VARS']['STRIPE_PUBLISHABLE_KEY'] = self.options['publishable_key']

        self.model_is_user = False
        if self.options["model"] is None and app.features.exists('users'):
            self.options["model"] = app.features.users.model
            self.options["email_attribute"] = app.features.users.options['email_column']
            self.model_is_user = True

        self.model = None
        if self.options["model"]:
            self.model = app.features.models.ensure_model(self.options["model"],
                stripe_customer_id=dict(type=str, index=True),
                stripe_subscription_id=dict(type=str, index=True),
                has_stripe_source=dict(type=bool, default=False, index=True),
                plan_name=dict(type=str, index=True),
                plan_status=dict(type=str, default='trialing', index=True),
                plan_last_charged_at=datetime.datetime,
                plan_last_charge_amount=float,
                plan_last_charge_successful=dict(type=bool, default=True, index=True),
                plan_next_charge_at=dict(type=datetime.datetime, index=True))
            self.model.stripe_customer = cached_property(self.find_model_customer)
            self.model.stripe_subscription = cached_property(self.find_model_subscription)
            self.model.stripe_default_source = cached_property(self.find_model_default_source)
            signal('stripe_customer_source_updated').connect(self.on_source_event)
            signal('stripe_customer_source_deleted').connect(self.on_source_event)
            signal('stripe_invoice_payment_succeeded').connect(self.on_invoice_payment)
            signal('stripe_invoice_payment_failed').connect(self.on_invoice_payment)
            signal('stripe_customer_subscription_updated').connect(self.on_subscription_event)
            signal('stripe_customer_subscription_deleted').connect(self.on_subscription_event)
            signal('stripe_customer_subscription_trial_will_end').connect(self.on_trial_will_end)

            if self.options['billing_fields']:
                app.features.models.ensure_model(self.model,
                    billing_name=str,
                    billing_address_line1=str,
                    billing_address_line2=str,
                    billing_address_city=str,
                    billing_address_state=str,
                    billing_address_zip=str,
                    billing_address_country=str,
                    billing_country=str,
                    billing_ip_address=str,
                    billing_brand=str,
                    billing_exp_month=str,
                    billing_exp_year=str,
                    billing_last4=str)

        if not self.model_is_user:
            self.model_is_user = self.model and app.features.exists('users') and self.model is app.features.users.model

        if self.options['eu_vat_support'] is None:
            self.options['eu_vat_support'] = app.features.exists('eu_vat')
        if self.options['eu_vat_support']:
            app.features.eu_vat.model_rate_updated_signal.connect(self.on_model_eu_vat_rate_update)

    def find_model_by_customer_id(self, cust_id):
        return current_app.features.models.query(self.model).filter(stripe_customer_id=cust_id).first()

    def find_model_by_subscription_id(self, subscription_id):
        return current_app.features.models.query(self.model).filter(stripe_subscription_id=subscription_id).first()

    def find_model_customer(self, obj):
        if not obj.stripe_customer_id:
            return
        try:
            return stripe.Customer.retrieve(obj.stripe_customer_id)
        except stripe.error.InvalidRequestError:
            if self.options['auto_create_customer']:
                return self.create_customer(obj, email=getattr(obj, self.options['email_attribute']))
            return

    def find_model_subscription(self, obj):
        if not obj.stripe_customer_id or not obj.stripe_subscription_id:
            return
        try:
            return obj.stripe_customer.subscriptions\
                .retrieve(obj.stripe_subscription_id)
        except stripe.error.InvalidRequestError:
            return

    def find_model_default_source(self, obj):
        if not obj.stripe_customer_id:
            return
        default_id = obj.stripe_customer.default_source
        if default_id:
            return obj.stripe_customer.sources.retrieve(default_id)

    def check_model(self, obj):
        if not obj.plan_name and self.options['must_have_plan'] and self.options['default_plan']:
            self.subscribe_plan(obj, self.options['default_plan'])
        if (not obj.plan_name and self.options['must_have_plan']) or \
           (obj.plan_name and ((obj.plan_status not in ('trialing', 'active') and not obj.has_stripe_source) or \
                                 obj.plan_status in ('canceled', 'unpaid'))):
            if self.options['no_payment_message']:
                flash(self.options['no_payment_message'], 'error')
            if self.options['no_payment_redirect_to']:
                return redirect(self.options['no_payment_redirect_to'])
            if self.options['add_source_view']:
                return redirect(url_for(self.options['add_source_view']))
        if obj.plan_status == 'past_due' and self.options['subscription_past_due_message']:
            flash(self.options['subscription_past_due_message'], 'error')

    @hook()
    def before_request(self):
        if request.endpoint in (self.options['add_source_view'], 'users.logout') or 'static' in request.endpoint:
            return
        if current_app.features.users.logged_in() and self.model_is_user:
            self.check_model(current_app.features.users.current)

    @action('stripe_model_create_customer', default_option='obj')
    @as_transaction
    def create_customer(self, obj, trial_end=None, coupon=None, tax_percent=None, **kwargs):
        if 'plan' in kwargs:
            kwargs.update(dict(trial_end=self._format_trial_end(trial_end),
                coupon=coupon, tax_percent=tax_percent))
        cust = stripe.Customer.create(**kwargs)
        self._update_model_customer(obj, cust)
        if 'plan' in kwargs:
            subscription = cust.subscriptions.data[0]
            self._update_model_subscription(obj, subscription)
        elif self.options['default_plan']:
            self.subscribe_plan(obj, self.options['default_plan'], trial_end=trial_end,
                coupon=coupon, tax_percent=tax_percent)
        save_model(obj)
        return cust

    @action('stripe_model_update_customer')
    @as_transaction
    def update_customer(self, obj, **kwargs):
        customer = obj.stripe_customer
        for k, v in kwargs.iteritems():
            setattr(customer, k, v)
        customer.save()
        self._update_model_customer(obj, customer)
        save_model(obj)

    @action('stripe_model_delete_customer')
    @as_transaction
    def delete_customer(self, obj, silent=True):
        if obj.stripe_customer:
            try:
                obj.stripe_customer.delete()
            except stripe.error.InvalidRequestError as e:
                if not silent or 'No such customer' not in e.message:
                    raise e
        self._update_model_customer(obj, None)
        if obj.stripe_subscription_id:
            self._update_model_subscription(obj, False)
        save_model(obj)

    def _update_model_customer(self, obj, cust):
        obj.stripe_customer_id = cust.id if cust else None
        obj.__dict__['stripe_customer'] = cust
        self._update_model_source(obj, cust)

    @action('stripe_model_add_source')
    @as_transaction
    def add_source(self, obj, token=None, **source_details):
        obj.stripe_customer.sources.create(source=token or source_details)
        obj.__dict__.pop('stripe_customer', None) # force refresh of customer object
        self._update_model_source(obj)
        save_model(obj)

    @action('stripe_model_add_source_from_form')
    def add_source_from_form(self, obj, form=None):
        form = current_context.data.get('form')
        if form and "stripeToken" in form:
            self.add_source(obj, form.stripeToken.data)
        elif "stripeToken" in request.form:
            self.add_source(obj, request.form['stripeToken'])
        elif form:
            self.add_source(obj,
                object="card",
                number=form.card_number.data,
                exp_month=form.card_exp_month.data,
                exp_year=form.card_exp_year.data,
                cvc=form.card_cvc.data,
                name=form.card_name.data)
        else:
            raise Exception("No form found to retrieve the stripeToken")

    @action('stripe_model_remove_source')
    @as_transaction
    def remove_source(self, obj, source_id=None):
        if not source_id:
            source_id = obj.stripe_customer.default_source
        try:
            source = obj.stripe_customer.sources.retrieve(source_id)
        except stripe.error.InvalidRequestError:
            return
        source.delete()
        obj.__dict__.pop('stripe_customer', None) # force refresh of customer object
        self._update_model_source(obj)

    def _update_model_source(self, obj, customer=None, store_ip_address=True):
        if not customer:
            customer = obj.stripe_customer
        obj.has_stripe_source = customer.default_source is not None if customer else False
        obj.__dict__.pop('stripe_default_source', None)
        if self.options['billing_fields'] and (obj.has_stripe_source or self.options['reset_billing_fields']):
            billing_fields = ('name', 'address_line1', 'address_line2', 'address_state', 'address_city',
                'address_zip', 'address_country', 'country', 'brand', 'exp_month', 'exp_year', 'last4')
            source = obj.stripe_default_source if obj.has_stripe_source else None
            for field in billing_fields:
                setattr(obj, 'billing_%s' % field, getattr(source, field) if source else None)
            if store_ip_address and obj.has_stripe_source:
                obj.billing_ip_address = request.remote_addr
        if self.options['eu_vat_support']:
            if self.options['eu_auto_vat_country']:
                country = None
                if obj.has_stripe_source:
                    if self.options['eu_vat_use_address_country']:
                        country = obj.stripe_default_source.address_country
                    else:
                        country = obj.stripe_default_source.country
                current_app.features.eu_vat.set_model_country(obj, country)
            if self.options['eu_auto_vat_rate'] and obj.stripe_subscription and obj.should_charge_eu_vat:
                self.update_subscription(obj, tax_percent=obj.eu_vat_rate)
        self.model_source_updated_signal.send(obj)

    @action('stripe_model_subscribe_plan')
    @as_transaction
    def subscribe_plan(self, obj, plan=None, quantity=1, **kwargs):
        if not plan:
            plan = self.options['default_plan']
        if obj.plan_name == plan:
            return
        params = dict(plan=plan, quantity=quantity,
            trial_end=self._format_trial_end(kwargs.pop('trial_end', None)))
        params.update(kwargs)
        if 'tax_percent' not in params:
            if self.options['eu_vat_support'] and self.options['eu_auto_vat_rate']:
                if obj.should_charge_eu_vat:
                    params['tax_percent'] = obj.eu_vat_rate
            elif self.options['default_subscription_tax_percent']:
                params['tax_percent'] = self.options['default_subscription_tax_percent']
        subscription = obj.stripe_customer.subscriptions.create(**params)
        self._update_model_subscription(obj, subscription)
        save_model(obj)
        return subscription

    def _format_trial_end(self, trial_end=None):
        if self.options['debug_trial_period'] and current_app.debug:
            if self.options['debug_trial_period'] == 'now':
                return 'now'
            else:
                trial_end = datetime.datetime.now() + \
                    datetime.timedelta(days=self.options['debug_trial_period'])
        if isinstance(trial_end, datetime.datetime):
            if trial_end <= datetime.datetime.now():
                return 'now'
            return int(time.mktime(trial_end.timetuple()))
        return trial_end

    @action('stripe_model_update_subscription')
    @as_transaction
    def update_subscription(self, obj, **kwargs):
        subscription = obj.stripe_subscription
        for k, v in kwargs.iteritems():
            setattr(subscription, k, v)
        subscription.save()
        self._update_model_subscription(obj, subscription)
        save_model(obj)

    @action('stripe_model_cancel_subscription', default_option='obj')
    @as_transaction
    def cancel_subscription(self, obj):
        obj.stripe_subscription.delete()
        self._update_model_subscription(obj, False)
        save_model(obj)

    def _update_model_subscription(self, obj, subscription=None):
        if subscription is None:
            if obj.stripe_customer.subscriptions.total_count > 0:
                subscription = obj.stripe_customer.subscriptions.data[0]
        prev_plan = obj.plan_name
        prev_status = obj.plan_status
        if subscription:
            obj.stripe_subscription_id = subscription.id
            obj.plan_name = subscription.plan.id
            obj.plan_status = subscription.status
            if obj.plan_status == 'trialing':
                obj.plan_next_charge_at = datetime.datetime.fromtimestamp(subscription.trial_end)
            elif subscription.current_period_end:
                obj.plan_next_charge_at = datetime.datetime.fromtimestamp(subscription.current_period_end)
            else:
                obj.plan_next_charge_at = None
        else:
            obj.stripe_subscription_id = None
            obj.plan_name = None
            obj.plan_status = 'canceled'
            obj.plan_next_charge_at = None
        self.model_subscription_updated_signal.send(obj, prev_plan=prev_plan, prev_status=prev_status)

    @as_transaction
    def update_last_subscription_charge(self, obj, invoice):
        obj.plan_last_charged_at = datetime.datetime.fromtimestamp(invoice.date)
        obj.plan_last_charge_amount = invoice.total / 100
        obj.plan_last_charge_successful = invoice.paid
        if invoice.paid:
            obj.plan_next_charge_at = datetime.datetime.fromtimestamp(obj.stripe_subscription.current_period_end)
        else:
            obj.plan_next_charge_at = datetime.datetime.fromtimestamp(invoice.next_payment_attempt)
        self.model_last_charge_updated_signal.send(obj)
        save_model(obj)

    @as_transaction
    def on_source_event(self, sender, stripe_event):
        source = stripe_event.data.object
        obj = self.find_model_by_customer_id(source.customer)
        if not obj:
            return
        self._update_model_source(obj, store_ip_address=False)
        save_model(obj)

    @as_transaction
    def on_subscription_event(self, sender, stripe_event):
        subscription = stripe_event.data.object
        obj = self.find_model_by_customer_id(subscription.customer)
        if not obj:
            return
        self._update_model_subscription(obj, subscription)
        save_model(obj)

    def on_trial_will_end(self, sender, stripe_event):
        subscription = stripe_event.data.object
        obj = self.find_model_by_subscription_id(subscription.id)
        if not obj:
            return
        if self.options['send_trial_will_end_email']:
            current_app.features.emails.send(getattr(obj, self.options['email_attribute']),
                'stripe/trial_will_end.txt', obj=obj)

    @as_transaction
    def on_invoice_payment(self, sender, stripe_event):
        invoice = stripe_event.data.object
        if not invoice.customer:
            return
        obj = self.find_model_by_customer_id(invoice.customer)
        if not obj or invoice.total == 0:
            return
        if invoice.subscription:
            sub_obj = None
            if obj.stripe_subscription_id == invoice.subscription:
                sub_obj = obj
            else:
                sub_obj = self.find_model_by_subscription_id(invoice.subscription)
            if sub_obj:
                self.update_last_subscription_charge(sub_obj, invoice)

        if self.options['eu_vat_support'] and self.options['billing_fields'] and\
          current_app.features.eu_vat.is_eu_country(obj.billing_country):
            invoice.metadata['eu_vat_exchange_rate'] = current_app.services.eu_vat.get_exchange_rate(
                obj.billing_country, invoice.currency.upper())
            if invoice.tax:
                invoice.metadata['eu_vat_amount'] = round(invoice.tax * invoice.metadata['eu_vat_exchange_rate'])
            if obj.eu_vat_number:
                invoice.metadata['eu_vat_number'] = obj.eu_vat_number
            invoice.save()

        self.invoice_payment_signal.send(invoice)
        if invoice.paid and current_app.features.exists('invoicing'):
            self.create_invoice_from_stripe(obj, invoice)
        elif not invoice.paid and self.options['send_failed_invoice_email']:
            self.send_failed_invoice_email(getattr(obj, self.options['email_attribute']), invoice)

    def create_invoice_from_stripe(self, obj, stripe_invoice):
        with current_app.features.invoicing.create(**self.options['invoice_ref_kwargs']) as invoice:
            invoice.customer = obj
            invoice.email = getattr(obj, self.options['email_attribute'])
            invoice.external_id = stripe_invoice.id
            invoice.currency = stripe_invoice.currency.upper()
            invoice.subtotal = stripe_invoice.subtotal / 100.0
            invoice.total = stripe_invoice.total / 100.0
            invoice.tax_rate = stripe_invoice.tax_percent
            invoice.tax_amount = stripe_invoice.tax / 100.0 if stripe_invoice.tax else None
            invoice.description = stripe_invoice.description
            invoice.issued_at = datetime.datetime.fromtimestamp(stripe_invoice.date)
            invoice.paid = stripe_invoice.paid
            invoice.charge_id = stripe_invoice.charge

            if self.options['billing_fields']:
                invoice.name = obj.billing_name
                invoice.address_line1 = obj.billing_address_line1
                invoice.address_line2 = obj.billing_address_line2
                invoice.address_city = obj.billing_address_city
                invoice.address_state = obj.billing_address_state
                invoice.address_zip = obj.billing_address_zip
                invoice.address_country = obj.billing_address_country
                invoice.country = obj.billing_country.upper()

            for line in stripe_invoice.lines.data:
                item = current_app.features.invoicing.item_model()
                item.external_id = line.id
                item.amount = line.amount / 100.0
                item.quantity = line.quantity
                item.currency = line.currency
                item.description = line.description
                if line.plan:
                    item.description = line.plan.statement_descriptor
                invoice.items.append(item)

    def send_failed_invoice_email(self, email, invoice, **kwargs):
        items = []
        for line in invoice.lines.data:
            desc = line.description or ''
            if line.plan:
                desc = line.plan.statement_descriptor
            items.append((desc, line.quantity, line.amount / 100.0))
        current_app.features.emails.send(email, 'failed_invoice.html',
            invoice_date=datetime.datetime.fromtimestamp(invoice.date),
            invoice_items=items,
            invoice_currency=invoice.currency.upper(),
            invoice_total=invoice.total / 100.0, **kwargs)

    def on_model_eu_vat_rate_update(self, sender):
        if sender.stripe_subscription:
            sender.stripe_subscription.tax_percent = sender.eu_vat_rate
            sender.stripe_subscription.save()