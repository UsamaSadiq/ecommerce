""" Stripe payment processing. """


import logging

import stripe
from oscar.apps.payment.exceptions import GatewayError, TransactionDeclined
from oscar.core.loading import get_model

from ecommerce.extensions.basket.models import Basket
from ecommerce.extensions.payment.constants import STRIPE_CARD_TYPE_MAP
from ecommerce.extensions.payment.processors import (
    ApplePayMixin,
    BaseClientSidePaymentProcessor,
    HandledProcessorResponse
)

logger = logging.getLogger(__name__)

BillingAddress = get_model('order', 'BillingAddress')
Country = get_model('address', 'Country')
PaymentEvent = get_model('order', 'PaymentEvent')
PaymentEventType = get_model('order', 'PaymentEventType')
PaymentProcessorResponse = get_model('payment', 'PaymentProcessorResponse')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')


class Stripe(ApplePayMixin, BaseClientSidePaymentProcessor):
    NAME = 'stripe'
    template_name = 'payment/stripe.html'

    def __init__(self, site):
        """
        Constructs a new instance of the Stripe processor.

        Raises:
            KeyError: If no settings configured for this payment processor.
        """
        super(Stripe, self).__init__(site)
        configuration = self.configuration

        # Stripe API version to use. Will use latest allowed in Stripe Dashboard if None.
        self.api_version = configuration['api_version']
        # Send anonymous latency metrics to Stripe.
        self.enable_telemetry = configuration['enable_telemetry']
        # Stripe client logging level. None will default to INFO.
        self.log_level = configuration['log_level']
        # How many times to automatically retry requests. None means no retries.
        self.max_network_retries = configuration['max_network_retries']
        # Send requests somewhere else instead of Stripe. May be useful for testing.
        self.proxy = configuration['proxy']
        # The key visible on the frontend to identify our Stripe account. Public.
        self.publishable_key = configuration['publishable_key']
        # The secret API key used by the backend to communicate with Stripe. Private/secret.
        self.secret_key = configuration['secret_key']

        stripe.api_key = self.secret_key
        stripe.log = self.log_level
        stripe.max_network_retries = self.max_network_retries
        stripe.proxy = self.proxy

    def _get_basket_amount(self, basket):
        """Convert to stripe amount, which is in cents."""
        return str((basket.total_incl_tax * 100).to_integral_value())

    def _build_payment_intent_parameters(self, basket):
        order_number = basket.order_number
        amount = self._get_basket_amount(basket)
        currency = basket.currency
        return {
            'amount': amount,
            'currency': currency,
            'description': order_number,
            'metadata': {'order_number': order_number},
            # put the order number on statements
            'statement_descriptor': order_number,
            'statement_descriptor_suffix': order_number,
        }

    def get_capture_context(self, request):
        # TODO: consider whether the basket should be passed in from MFE, not retrieved from Oscar
        basket = Basket.get_basket(request.user, request.site)

        # TODO: handle stripe.error.IdempotencyError when basket was already created, but with different amount
        create_api_response = stripe.PaymentIntent.create(
            **self._build_payment_intent_parameters(basket),
            # only allow backend to submit payments
            confirmation_method='manual',
            # don't create a new intent for the same basket
            idempotency_key=basket.order_number,
        )

        transaction_id = create_api_response['id']
        self.record_processor_response(create_api_response, transaction_id)
        new_capture_context = {'key_id': create_api_response['client_secret']}
        return new_capture_context

    def get_transaction_parameters(self, basket, request=None, use_client_side_checkout=True, **kwargs):
        return {'payment_page_url': self.client_side_payment_url}

    def handle_processor_response(self, response, basket=None):
        payment_intent_id = response

        # NOTE: In the future we may want to get/create a Customer. See https://stripe.com/docs/api#customers.
        try:
            modify_api_response = stripe.PaymentIntent.modify(
                payment_intent_id,
                **self._build_payment_intent_parameters(basket),
            )

            # NOTE: PaymentIntent objects subclass the dict class so there is no need to do any data transformation
            # before storing the response in the database.

            self.record_processor_response(modify_api_response, transaction_id=payment_intent_id, basket=basket)

            confirm_api_response = stripe.PaymentIntent.confirm(
                payment_intent_id,
                # stop on complicated payments MFE can't handle yet
                error_on_requires_action=True,
            )

            self.record_processor_response(confirm_apiresponse, transaction_id=payment_intent_id, basket=basket)

            logger.info('Successfully created Stripe payment intent [%s] for basket [%d].', transaction_id, basket.id)

        except stripe.error.CardError as ex:
            base_message = "Stripe payment for basket [%d] declined with HTTP status [%d]"
            exception_format_string = "{}: %s".format(base_message)
            body = ex.json_body
            logger.exception(
                exception_format_string,
                basket.id,
                ex.http_status,
                body
            )
            self.record_processor_response(body, basket=basket)
            raise TransactionDeclined(base_message, basket.id, ex.http_status) from ex

        # proceed only if payment went through
        assert confirm_api_response.status == "succeeded"

        total = basket.total_incl_tax
        currency = basket.currency
        card_object = confirm_api_response.charges.data[0].payment_method_details.card
        card_number = card_object.last4
        card_type = STRIPE_CARD_TYPE_MAP.get(card_object.brand)

        return HandledProcessorResponse(
            transaction_id=transaction_id,
            total=total,
            currency=currency,
            card_number=card_number,
            card_type=card_type
        )

    def issue_credit(self, order_number, basket, reference_number, amount, currency):
        try:
            refund = stripe.Refund.create(charge=reference_number)
        except:
            msg = 'An error occurred while attempting to issue a credit (via Stripe) for order [{}].'.format(
                order_number)
            logger.exception(msg)
            raise GatewayError(msg)  # pylint: disable=raise-missing-from

        transaction_id = refund.id

        # NOTE: Refund objects subclass dict so there is no need to do any data transformation
        # before storing the response in the database.
        self.record_processor_response(refund, transaction_id=transaction_id, basket=basket)

        return transaction_id
