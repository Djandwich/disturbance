from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
from disturbance.components.compliances.models import Compliance
from ledger.accounts.models import EmailUser
import datetime



import itertools

class Command(BaseCommand):
    help = 'Change the status of Approvals to Expired when past expiry date'

    def handle(self, *args, **options):
        try:
            user = EmailUser.objects.get(email__icontains='cron')
        except:
            user = user = EmailUser.objects.create(email='cron@dbca.wa.gov.au', password = '')

        today = timezone.now().date()
        for c in Compliance.objects.filter(processing_status = 'due'):
            if c.due_date < today:
                if c.lodgement_date==None:
                    try:
                        c.send_reminder(user)
                        c.save()
                        print('Reminder sent for Compliance {} '.format(c.id))
                    except:
                        print('Error sending Reminder Compliance {} '.format(c.id))