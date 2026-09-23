import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch
from app.config import ConfigError
from app.service_profile import validate, load_profile
from app.install_config import collect, save_new
from app.main import main


def profile():
    return {'version':1,'environment':{'MAX_BOT_TOKEN':'test-secret','WORK_CHAT_ID':'-30',
        'WEBHOOK_SECRET':'webhook-secret','DATABASE_PATH':'data/profile_test.db','LISTEN_HOST':'127.0.0.1',
        'LISTEN_PORT':'8080','TIMEZONE':'Europe/Moscow'},
        'policy':{'client_chats':[-20],'specialists':[99],'admins':[77],'timeout_seconds':300},
        'public_webhook_url':'https://example.org/webhook/max'}


class InstallationTests(unittest.TestCase):
    def test_profile_validates_roles_ids_and_ignores_environment(self):
        with patch.dict('os.environ',{'MAX_BOT_TOKEN':'wrong','WORK_CHAT_ID':'-999'}):
            settings, policy=validate(profile())
        self.assertEqual(settings.token,'test-secret')
        self.assertEqual(policy.work_chat,-30)
        self.assertEqual(policy.client_chats,frozenset({-20}))
        self.assertNotIn('test-secret',repr(settings))
        for key,value in [('client_chats',[-30]),('admins',[]),('specialists',[True]),('timeout_seconds',0)]:
            data=profile();data['policy'][key]=value
            with self.assertRaises(ConfigError):validate(data)

    def test_profile_bad_json_and_secret_errors_are_sanitized(self):
        for raw in ({},[],{'version':True}, {'version':1,'environment':'SECRET'}):
            with self.assertRaises(ConfigError) as error:validate(raw)
            self.assertNotIn('SECRET',str(error.exception))
        data=profile();data['public_webhook_url']='https://user:SECRET@example.org/webhook'
        with self.assertRaises(ConfigError) as error:validate(data)
        self.assertNotIn('SECRET',str(error.exception))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'bad.json';path.write_text('{SECRET',encoding='utf-8')
            with self.assertRaises(ConfigError) as error:load_profile(path)
            self.assertNotIn('SECRET',str(error.exception))

    def test_wizard_collects_and_generates_secret_without_echo(self):
        answers=iter(['-30','-20,-21','77','99','','','','','','','https://example.org/webhook/max'])
        secrets=iter(['TOKEN',''])
        prompts=[]
        data=collect(lambda prompt:(prompts.append(prompt),next(answers))[1],lambda prompt:next(secrets))
        self.assertEqual(data['policy']['client_chats'],[-20,-21])
        self.assertEqual(data['environment']['MAX_BOT_TOKEN'],'TOKEN')
        self.assertGreaterEqual(len(data['environment']['WEBHOOK_SECRET']),32)
        self.assertNotIn('TOKEN',''.join(prompts))
        self.assertEqual(data['environment']['LISTEN_HOST'],'127.0.0.1')

    def test_save_is_exclusive_and_roundtrips_literal_shell_characters(self):
        data=profile();data['environment']['MAX_BOT_TOKEN']='literal$()`%token'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json'
            save_new(path,data)
            self.assertEqual(load_profile(path)[0].token,data['environment']['MAX_BOT_TOKEN'])
            before=path.read_bytes()
            with self.assertRaises(FileExistsError):save_new(path,profile())
            self.assertEqual(path.read_bytes(),before)

    def test_main_profile_check_and_no_secret_output_or_database_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            data=profile();db=Path(directory)/'data.db';data['environment']['DATABASE_PATH']=str(db)
            path=Path(directory)/'config.json';save_new(path,data)
            output=io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(['check-config','--profile',str(path)]),0)
                self.assertEqual(main(['run','--profile',str(path),'--client-chat=-21']),1)
            self.assertNotIn('test-secret',output.getvalue())
            self.assertFalse(db.exists())

    def test_main_profile_run_uses_persistent_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json';save_new(path,profile())
            with patch('app.main.create_app',return_value='app') as create,patch('app.main.web.run_app') as run,patch('app.main.logging.basicConfig'),redirect_stdout(io.StringIO()):
                self.assertEqual(main(['run','--profile',str(path)]),0)
            selected=create.call_args.kwargs['policy']
            self.assertEqual(selected.admins,frozenset({77}))
            self.assertEqual(selected.specialists,frozenset({99}))
            loop=run.call_args.kwargs['loop']
            if loop is not None:loop.close()
