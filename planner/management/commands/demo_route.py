import json
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.test import Client

class Command(BaseCommand):
    help = 'Exercise the real routing provider twice and save a Postman-style demo response.'
    def handle(self, *args, **options):
        client = Client()
        payload = {'start': 'Chicago, IL', 'finish': 'Los Angeles, CA'}
        for attempt in range(2):
            response = client.post('/api/route/', json.dumps(payload), content_type='application/json')
            data = response.json()
            if response.status_code != 200:
                raise CommandError(f'{response.status_code}: {data}')
            Path('docs/example-response.json').write_text(json.dumps(data, indent=2))
            self.stdout.write(json.dumps({k: data[k] for k in ['distance_miles', 'total_cost_usd', 'metadata']}))
            self.stdout.write(f"Fuel stops: {len(data['fuel_stops'])}")
