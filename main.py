import pickle
from twocaptcha import TwoCaptcha
from json import load, JSONDecodeError
from os.path import exists
from os import remove
from urllib.parse import urljoin
from exceptions import ConfigMissing, CorruptJSON, TwoCaptchaKeyNotFound, CaptchaNotSolved, LoggedOut
from retry import retry
from pyppeteer.errors import ElementHandleError
from pyppeteer.element_handle import ElementHandle
from typing import List, Dict, Union
from lxml.html import HtmlElement
import speech_recognition as sr
from pydub import AudioSegment
import pyppeteer

class PageAllCookies(pyppeteer.page.Page):
    async def allCookies(self) -> List[Dict[str, Union[str, int, bool]]]:
        resp = await self._client.send('Network.getAllCookies')
        return resp.get('cookies', {})

pyppeteer.page.Page = PageAllCookies
from requests_html import HTMLSession, HTML

CONFIG_PATH = 'config.json'


class IMDB(HTMLSession):
	_site: str = 'https://{}.imdb.com'
	_current_url: str = ""
	_settings: dict = {}
	_html: HTML = None
	
	@property
	def settings(self):
		if not self._settings:
			if not exists(CONFIG_PATH):
				raise ConfigMissing("Program not able to find provided configuration file")
			with open(CONFIG_PATH) as file:
				try:
					self._settings = load(file)
				except JSONDecodeError as error:
					raise CorruptJSON("Programm not able to recognize content of configuration file")
		return self._settings

	@property
	def email(self):
		return self.settings.get('email')

	@property
	def password(self):
		return self.settings.get('password')

	@property
	def site(self):
		return self._site.format('www')
	@property
	def pro(self):
		return self._site.format('pro')

	@property
	def registration_path(self):
		return urljoin(self.site, "/registration/signin?ref=nv_generic_lgin&u=%2F")
	
	@property
	def signin_path(self):
		return urljoin(self.site, "/ap/signin")
	
	@property
	def current_url(self):
		return self._current_url

	@current_url.setter
	def current_url(self, value):
		self._current_url = value	

	@property
	def html(self):
		return self._html
	
	@html.setter
	def html(self, value):
		self._html = value	

	@property
	def page(self):
		return self.html.page
	
	def export_cookies(self):
		with open('cookies.pickle', 'wb') as file:
			pickle.dump(self.cookies, file)
	
	def load_cookies(self):
		if exists('cookies.pickle'):
			with open('cookies.pickle', 'rb') as file:
				self.cookies.update(pickle.load(file))
			return True
		return False

	def solve(self):
		if not self.settings.get('2CAPTCHA_KEY'):
			raise TwoCaptchaKeyNotFound
		solver = TwoCaptcha(self.settings.get('2CAPTCHA_KEY'))
		result = solver.normal('captcha.jpg')
		return result
	
	def wait(self, func):
		return self.loop.run_until_complete(func)
	
	def screenshot(self):
		self.wait(self.page.screenshot(path="screenshot.png"))
	
	def update_cookies(self):
		cookies = self.cookies
		for cookie in cookies:
			self.wait(self.page.setCookie(dict(name=cookie.name, value=cookie.value, domain=cookie.domain)))

	def save_cookies(self):
		cookies = self.wait(self.page.allCookies())
		self.cookies.clear()
		for cookie in cookies:
			self.cookies.set(name=cookie['name'], value=cookie['value'], domain=cookie['domain'])

	def get(self, url, *args, **kwargs):
		ignore = kwargs.pop('ignore', False)
		response = super().get(url, *args, **kwargs)
		if not ignore:
			self.current_url = url
			self.html = response.html
		return response
	
	def post(self, url, *args, **kwargs):
		ignore = kwargs.pop('ignore', False)
		response = super().post(url, *args, **kwargs)
		if not ignore:
			self.current_url = url
			self.html = response.html
		return response
	
	def parent(self, child):
		return child.element.getparent()

	def get_attr(self, element, key):
		if isinstance(element, list):
			for el in element:
				if (value:=el.get(key,'!')) != '!':
					return value
		else:
			if isinstance(element, HtmlElement):
				return element.get(key)
			elif isinstance(element, ElementHandle):
				return self.wait(self.wait(element.getProperty(key)).jsonValue())
			else:
				return element.attrs.get(key)

	def visit_signin_page(self):
		url = self.registration_path
		self.get(url)
		imdb_button = self.parent(self.html.find('.auth-sprite.imdb-logo.retina', first=True))
		return self.get_attr(imdb_button, 'href')
	
	@retry(ElementHandleError, tries=3, delay=0)
	def get_encrypted_form(self):
		url = self.visit_signin_page()
		self.get(url)
		script = """formE=document.querySelector('form[name="signIn"]');formE.querySelector('input[name="email"]').value="%s";formE.querySelector('input[name="password"]').value = "%s";form = window.SiegeCrypto.createFormHandler(formE);form.configure({formProfile: 'AuthenticationPortalSigninNA',encryptionContext: {}});form.generateProcessedForm({}).then(function(e){return (e.querySelector('input[name="password"]').value);})"""
		script = script % (self.email, self.password)
		password = self.html.render(script=script,reload=False, sleep=2, keep_page=True)
		form = self.html.find('form[name=signIn]')
		inputs = {}
		for input_field in form.find('input'):
			name = self.get_attr(input_field, 'name')
			if name == 'email':
				inputs[name] = self.email
			elif name == 'password':
				inputs['encryptPwd'] = password
			elif name:
				inputs[name] = self.get_attr(input_field, 'value') or ''
		return inputs

	async def content(self):
		return await self.page.content()

	def fill_form(self):
		self.wait(self.page.evaluate('document.querySelector("input[name=email]").value=""'))
		self.wait(self.page.type("input[name=email]", self.email))
		self.wait(self.page.evaluate('document.querySelector("input[name=password]").value=""'))
		self.wait(self.page.type("input[name=password]", self.password))
		self.wait(self.page.click('#signInSubmit'))
		self.wait(self.page.waitForNavigation())

	def login(self):
		url = self.visit_signin_page()
		self.get(url)
		self.html.render(keep_page=True)
		self.update_cookies()
		self.wait(self.page.reload())
		self.fill_form()
		self.save_cookies()
		captcha = False
		if captcha_img:=self.wait(self.page.querySelector('#auth-captcha-image')):
			captcha_response = self.get(self.get_attr(captcha_img, 'src'), ignore=True)
			with open('captcha.jpg', 'wb') as file:
				file.write(captcha_response.content)
			result = self.solve()['code']
			remove('captcha.jpg')
			captcha = True 
		elif audio_btn:=self.wait(self.page.querySelector('a#auth-switch-captcha-to-audio')):
			captcha_response = self.get(self.get_attr(audio_btn, 'href'))
			audio_link = self.get_attr(self.html.find('#mp3-file', first=True), 'src')
			response = self.get(audio_link, ignore=True)
			with open('audio.mp3', 'wb') as file:
				file.write(response.content)
			r = sr.Recognizer()
			sound = AudioSegment.from_mp3('audio.mp3')
			sound.export("audio.wav", format="wav")
			remove('audio.mp3')
			with sr.AudioFile('audio.wav') as source2:
				r.adjust_for_ambient_noise(source2, duration=0.5)
				audio2 = r.record(source2)
				result = r.recognize_google(audio2)
			remove('audio.wav')
			self.html.render(keep_page=True)
			captcha = True
		if captcha:
			self.wait(self.page.type('#auth-captcha-guess', result))
			self.fill_form()
			self.save_cookies()
		if self.wait(self.page.querySelector('#auth-captcha-image')):
			raise CaptchaNotSolved
		self.screenshot()
		self.current_url = self.page.url
		assert '/ap/signin' not in self.current_url
		self.export_cookies()
	
	@retry(LoggedOut, tries=2, delay=0)
	def start(self):
		if not self.load_cookies():
			self.login()
		self.get(self.site)
		user = self.html.find('.navbar__user-name', first=True)
		if not user:
			if exists('cookies.pickle'):
				remove('cookies.pickle')
			raise LoggedOut
		print(user.text)

imdb = IMDB()
imdb.start()
