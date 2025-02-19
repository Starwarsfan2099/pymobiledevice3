import asyncio
import logging
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from functools import update_wrapper
from typing import Optional, Type

import click
import inquirer
import IPython
import uvicorn
from inquirer.themes import GreenPassion
from prompt_toolkit import HTML, PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import style_from_pygments_cls
from pygments import formatters, highlight, lexers
from pygments.styles import get_style_by_name

from pymobiledevice3.cli.cli_common import Command, wait_return
from pymobiledevice3.common import get_home_folder
from pymobiledevice3.exceptions import InspectorEvaluateError, LaunchingApplicationError, \
    RemoteAutomationNotEnabledError, WebInspectorNotEnabledError, WirError
from pymobiledevice3.lockdown import LockdownClient
from pymobiledevice3.services.web_protocol.cdp_server import app
from pymobiledevice3.services.web_protocol.driver import By, Cookie, WebDriver
from pymobiledevice3.services.web_protocol.inspector_session import InspectorSession
from pymobiledevice3.services.webinspector import SAFARI, Page, WebinspectorService

logger = logging.getLogger(__name__)


@click.group()
def cli():
    """ webinspector cli """
    pass


@cli.group()
def webinspector():
    """ webinspector options """
    pass


def catch_errors(func):
    def catch_function(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except LaunchingApplicationError:
            logger.error('Unable to launch application (try to unlock device)')
        except WebInspectorNotEnabledError:
            logger.error('Web inspector is not enable')
        except RemoteAutomationNotEnabledError:
            logger.error('Remote automation is not enable')

    return update_wrapper(catch_function, func)


def reload_pages(inspector: WebinspectorService):
    inspector.get_open_pages()
    # Best effort.
    inspector.flush_input(2)


def create_webinspector_and_launch_app(lockdown: LockdownClient, timeout: float, app: str):
    inspector = WebinspectorService(lockdown=lockdown)
    inspector.connect(timeout)
    application = inspector.open_app(app)
    return inspector, application


@webinspector.command(cls=Command)
@click.option('-v', '--verbose', is_flag=True)
@click.option('-t', '--timeout', default=3, show_default=True, type=float)
@catch_errors
def opened_tabs(lockdown: LockdownClient, verbose, timeout):
    """
    Show All opened tabs.
    Opt in:

        Settings -> Safari -> Advanced -> Web Inspector
    """
    inspector = WebinspectorService(lockdown=lockdown, loop=asyncio.get_event_loop())
    inspector.connect(timeout)
    while not inspector.connected_application:
        inspector.flush_input()
    reload_pages(inspector)
    for app_id, app_ in inspector.connected_application.items():
        if app_id not in inspector.application_pages:
            continue
        if verbose:
            print(f'{app_.name}    id: {app_id}')
        else:
            print(app_.name)
        for page_id, page in inspector.application_pages[app_id].items():
            if verbose:
                print(f' - {page.web_url}    id: {page_id}')
            else:
                print(f' - {page.web_url}')
    inspector.close()


@webinspector.command(cls=Command)
@click.argument('url')
@click.option('-t', '--timeout', default=3, show_default=True, type=float)
@catch_errors
def launch(lockdown: LockdownClient, url, timeout):
    """
    Open a specific URL in Safari.
    Opt in:

        Settings -> Safari -> Advanced -> Web Inspector

        Settings -> Safari -> Advanced -> Remote Automation
    """
    inspector, safari = create_webinspector_and_launch_app(lockdown, timeout, SAFARI)
    session = inspector.automation_session(safari)
    driver = WebDriver(session)
    print('Starting session')
    driver.start_session()
    print('Getting URL')
    driver.get(url)
    wait_return()
    session.stop_session()
    inspector.close()


SHELL_USAGE = '''
# This shell allows you to control the web with selenium like API.
# The first thing you should do is creating a session:
driver.start_session()

# Then, you can navigate by using the get method:
driver.get("https://google.com")

# You can search for a specific element using the `By` enum:
driver.find_element(By.TAG_NAME, 'input')

# You can also add cookies:
driver.add_cookie(
    Cookie(name='tz', value='UTC', domain='.github.com', path='/', expires=0, httpOnly=False, secure=True,
    session=True, sameSite='None')
)

# See selenium api for more features.
'''


@webinspector.command(cls=Command)
@click.option('-t', '--timeout', default=3, show_default=True, type=float)
@catch_errors
def shell(lockdown: LockdownClient, timeout):
    """
    Opt in:

        Settings -> Safari -> Advanced -> Web Inspector

        Settings -> Safari -> Advanced -> Remote Automation
    """
    inspector, safari = create_webinspector_and_launch_app(lockdown, timeout, SAFARI)
    session = inspector.automation_session(safari)
    driver = WebDriver(session)
    try:
        IPython.embed(
            header=highlight(SHELL_USAGE, lexers.PythonLexer(), formatters.TerminalTrueColorFormatter(style='native')),
            user_ns={
                'driver': driver,
                'Cookie': Cookie,
                'By': By,
            })
    finally:
        session.stop_session()
        inspector.close()


@webinspector.command(cls=Command)
@click.option('-t', '--timeout', default=3, show_default=True, type=float)
@click.option('--automation', is_flag=True, help='Use remote automation')
@click.argument('url', required=False, default='')
@catch_errors
def js_shell(lockdown: LockdownClient, timeout, automation, url):
    """
    Opt in:

        Settings -> Safari -> Advanced -> Web Inspector

    for automation also enable:

        Settings -> Safari -> Advanced -> Remote Automation
    """

    js_shell_class = AutomationJsShell if automation else InspectorJsShell
    asyncio.run(run_js_shell(js_shell_class, lockdown, timeout, url))


udid = ''


def create_app():
    inspector = WebinspectorService(lockdown=LockdownClient(udid))
    app.state.inspector = inspector
    return app


@webinspector.command(cls=Command)
@click.option('--host', default='127.0.0.1')
@click.option('--port', type=click.INT, default=9222)
def cdp(lockdown: LockdownClient, host, port):
    global udid
    udid = lockdown.udid
    uvicorn.run('pymobiledevice3.cli.webinspector:create_app', host=host, port=port, factory=True,
                ws_ping_timeout=None, ws='wsproto', loop='asyncio')


class JsShell(ABC):

    def __init__(self):
        super().__init__()
        self.prompt_session = PromptSession(lexer=PygmentsLexer(lexers.JavascriptLexer),
                                            auto_suggest=AutoSuggestFromHistory(),
                                            style=style_from_pygments_cls(get_style_by_name('stata-dark')),
                                            history=FileHistory(self.webinspector_history_path()))

    @classmethod
    @abstractmethod
    def create(cls, lockdown: LockdownClient, timeout: float, app: str):
        pass

    @abstractmethod
    async def evaluate_expression(self, exp):
        pass

    @abstractmethod
    async def navigate(self, url: str):
        pass

    async def js_iter(self):
        with patch_stdout(True):
            exp = await self.prompt_session.prompt_async(HTML('<style fg="cyan"><b>&gt;</b></style> '))

        if not exp.strip():
            return

        result = await self.evaluate_expression(exp)
        colorful_result = highlight(f'{result}', lexers.JavascriptLexer(),
                                    formatters.TerminalTrueColorFormatter(style='stata-dark'))
        print(colorful_result, end='')

    async def start(self, url: str = ''):
        if url:
            await self.navigate(url)
        while True:
            try:
                await self.js_iter()
            except WirError as e:
                logger.error(e)
            except (KeyboardInterrupt, InspectorEvaluateError):  # KeyboardInterrupt Control-C
                pass
            except EOFError:  # Control-D
                return

    @staticmethod
    def webinspector_history_path() -> str:
        return str(get_home_folder() / 'webinspector_history')


class AutomationJsShell(JsShell):

    def __init__(self, driver: WebDriver):
        super().__init__()
        self.driver = driver

    @classmethod
    @asynccontextmanager
    async def create(cls, lockdown: LockdownClient, timeout: float, app: str) -> 'AutomationJsShell':
        inspector, application = create_webinspector_and_launch_app(lockdown, timeout, app)
        automation_session = inspector.automation_session(application)
        driver = WebDriver(automation_session)
        driver.start_session()
        try:
            yield cls(driver)
        finally:
            automation_session.stop_session()
            inspector.close()

    async def evaluate_expression(self, exp: str):
        return self.driver.execute_script(f'return {exp}')

    async def navigate(self, url: str):
        self.driver.get(url)


class InspectorJsShell(JsShell):

    def __init__(self, inspector_session: InspectorSession):
        super().__init__()
        self.inspector_session = inspector_session

    @classmethod
    @asynccontextmanager
    async def create(cls, lockdown: LockdownClient, timeout: float, app: str) -> 'InspectorJsShell':
        inspector, application = create_webinspector_and_launch_app(lockdown, timeout, app)
        page = InspectorJsShell.query_page(inspector)
        if page is None:
            raise click.exceptions.Exit()

        inspector_session = await inspector.inspector_session(application, page)
        await inspector_session.runtime_enable()
        try:
            yield cls(inspector_session)
        finally:
            inspector.close()

    async def evaluate_expression(self, exp: str):
        return await self.inspector_session.runtime_evaluate(exp)

    async def navigate(self, url: str):
        await self.inspector_session.navigate_to_url(url)

    @staticmethod
    def query_page(inspector: WebinspectorService) -> Optional[Page]:
        reload_pages(inspector)
        available_pages = list(inspector.get_open_pages().get('Safari', []))
        if not available_pages:
            logger.error('Unable to find available pages (try to unlock device)')
            return

        page_query = [inquirer.List('page', message='choose page', choices=available_pages, carousel=True)]
        page = inquirer.prompt(page_query, theme=GreenPassion(), raise_keyboard_interrupt=True)['page']
        return page


async def run_js_shell(js_shell_class: Type[JsShell], lockdown: LockdownClient,
                       timeout: float, url: str):
    async with js_shell_class.create(lockdown, timeout, SAFARI) as js_shell_instance:
        await js_shell_instance.start(url)
