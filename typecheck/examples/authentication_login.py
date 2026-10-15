from lm15 import provider_descriptors
from lm15.login import Auth, TerminalUI

auth = Auth.local()
auth.login(provider_descriptors.xai.id, "device", ui=TerminalUI())
