from lm15 import providers
from lm15.login import Auth, TerminalUI

auth = Auth.local()
auth.login(providers.xai.id, "device", ui=TerminalUI())
