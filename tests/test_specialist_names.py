import json
from test_actions import ActionTests


class SpecialistNameTests(ActionTests):
    async def test_named_specialist_and_client_statuses(self):
        with self.store.connect() as conn:
            self.assertIn('Заявка ожидает специалиста 👨‍🔧', conn.execute("SELECT text FROM outbox WHERE destination='client'").fetchone()[0])
        await self.click()
        with self.store.connect() as conn:
            raw = json.loads(conn.execute("SELECT payload_json FROM inbox_events WHERE update_type='message_callback'").fetchone()[0])
            raw['callback']['user'].update(first_name='Денис', last_name='Горностаев')
            conn.execute("UPDATE inbox_events SET payload_json=? WHERE update_type='message_callback'", (json.dumps(raw),))
        await self.processor.tick()
        with self.store.connect() as conn:
            cards = dict(conn.execute("SELECT destination,text FROM outbox WHERE destination IN ('work','client')"))
        self.assertIn('Специалист: Денис Горностаев', cards['work'])
        self.assertIn('Заявка в работе у 👨‍🔧 "Денис Горностаев"', cards['client'])
        await self.click('done', rev=2, click='done-name')
        await self.processor.tick()
        with self.store.connect() as conn:
            client = conn.execute("SELECT text FROM outbox WHERE destination='client'").fetchone()[0]
        self.assertIn('Заявка закрыта. Спасибо за обращение!👨‍🔧 "Денис Горностаев"', client)
