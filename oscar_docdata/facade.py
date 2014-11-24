"""
Bridging module between Oscar and the gateway module (which is Oscar agnostic)
"""
import logging
from django.utils.translation import get_language
from oscar.apps.payment.exceptions import PaymentError
from oscar_docdata import appsettings
from oscar_docdata.compat import get_model
from oscar_docdata.exceptions import DocdataCreateError
from oscar_docdata.gateway import Name, Shopper, Destination, Address, Amount, to_iso639_part1
from oscar_docdata.interface import Interface

logger = logging.getLogger(__name__)

Order = None
SourceType = None

def _lazy_get_models():
    # This avoids various import conflicts between apps that may
    # import the Facade before any other models.
    global Order
    global SourceType
    if Order is None:
        Order = get_model('order', 'Order')
        SourceType = get_model('payment', 'SourceType')


class Facade(Interface):
    """
    The bridge between Oscar and the generic Interface.

    Most methods are just called directly on the Interface.
    """

    def create_payment(self, order_number, total, user, language=None, description=None, profile=None, **kwargs):
        """
        Start a new payment session / container.
        Besides the overwritten parameters, also provide:

        :param total: The total price
        :type total: :class:`oscar.core.prices.Price`
        :param billingaddress: The shipping address.
        :type billingaddress: :class:`oscar.apps.order.models.BillingAddress`
        """
        if not profile:
            profile = appsettings.DOCDATA_PROFILE

        try:
            order_key = super(Facade, self).create_payment(order_number, total, user, language=language, description=description, profile=profile, **kwargs)
        except DocdataCreateError as e:
            raise PaymentError(e.value, e)

        return order_key



    def get_create_payment_args(self, order_number, total, user, language=None, description=None, profile=None, **kwargs):
        """
        The arguments for the createpayment call.
        This is a separate method to be easily overwritable.
        """
        billingaddress = kwargs['billingaddress']

        if not profile:
            profile = appsettings.DOCDATA_PROFILE

        shopper_name = Name(
            first=user.first_name,
            last=user.last_name
        )

        bill_to_name = Name(
            first=billingaddress.first_name or user.first_name,
            last=billingaddress.last_name or user.last_name
        )

        return dict(
            order_id=order_number,
            total_gross_amount=Amount(total.incl_tax, total.currency),
            shopper=Shopper(
                id=user.id,
                name=shopper_name,
                email=user.email,
                language=to_iso639_part1(language or get_language()),
                gender='U'
            ),
            bill_to=Destination(
                bill_to_name,
                address=Address(                            # NOTE: oscar has no street / housenumber fields!
                    street=billingaddress.line1[:32],       # Docdata has a 32 char limit on street
                    house_number='N/A',                     # Field is required! Could consider passing nbsp or line2 ('\xc2\xa0')
                    house_number_addition=None,
                    postal_code=billingaddress.postcode,
                    city=billingaddress.city,
                    state=billingaddress.state,
                    country_code=billingaddress.country_id  # The Country.iso_3166_1_a2 field.
                )
            ),
            description=description,
            profile=profile
        )


    def order_status_changed(self, docdataorder, old_status, new_status):
        """
        The order status changed.
        """
        _lazy_get_models()
        project_status = appsettings.DOCDATA_ORDER_STATUS_MAPPING.get(new_status, new_status)
        cascade = appsettings.OSCAR_ORDER_STATUS_CASCADE.get(project_status, None)

        # Update the order in Oscar
        # Using select_for_update() to have a lock on the order first.
        order = Order.objects.select_for_update().get(number=docdataorder.merchant_order_id)
        if order.status == project_status:
            # Parallel update by docdata (return URL and callback), avoid sending the signal twice to the user code.
            logging.info("Order {0} status is already {1}, skipping signal.".format(order.number))
            return

        # Not using Order.set_status(), forcefully set it to the current situation.
        order.status = project_status
        if cascade:
            order.lines.all().update(status=cascade)
        order.save()

        # Send the signal
        super(Facade, self).order_status_changed(docdataorder, old_status, new_status)


    def get_source_type(self):
        """
        Convenience method, return the canonical SourceType for Docdata payment events.
        """
        _lazy_get_models()
        source_type, _ = SourceType.objects.get_or_create(code='docdata', defaults={'name': "Docdata Payments"})
        return source_type
