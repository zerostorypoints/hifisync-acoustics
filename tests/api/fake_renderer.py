"""A minimal UPnP media renderer on localhost: a device description and an AVTransport
control URL that records the SOAP actions it receives."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
 <device>
  <deviceType>urn:schemas-upnp-org:device:MediaRenderer:2</deviceType>
  <friendlyName>Test Amp</friendlyName><modelName>TA1</modelName>
  <serviceList>
   <service><serviceType>urn:schemas-upnp-org:service:RenderingControl:2</serviceType>
    <controlURL>/rc</controlURL></service>
   {avtransport}
  </serviceList>
 </device>
</root>"""
TRANSPORT_INFO = (
    '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
    '<s:Body><u:GetTransportInfoResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:2">'
    "<CurrentTransportState>{state}</CurrentTransportState>"
    "<CurrentTransportStatus>OK</CurrentTransportStatus><CurrentSpeed>1</CurrentSpeed>"
    "</u:GetTransportInfoResponse></s:Body></s:Envelope>")
AVT = ("<service><serviceType>urn:schemas-upnp-org:service:AVTransport:2</serviceType>"
       "<controlURL>/ctl/avt</controlURL></service>")


class FakeRenderer:
    def __init__(self, with_avtransport=True, refuse=False, states=None):
        """``states``: CurrentTransportState answers to GetTransportInfo, in order (the last one
        repeats); None answers GetTransportInfo with an empty body, as a renderer without it."""
        self.actions = []  # (path, SOAPACTION header, body)
        self.states = list(states) if states else None
        outer = self
        desc = DESCRIPTION.format(avtransport=AVT if with_avtransport else "")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = desc.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                action = self.headers.get("SOAPACTION", "")
                outer.actions.append((self.path, action, self.rfile.read(n).decode()))
                body = b""
                if action.endswith('#GetTransportInfo"') and outer.states:
                    state = outer.states.pop(0) if len(outer.states) > 1 else outer.states[0]
                    body = TRANSPORT_INFO.format(state=state).encode()
                self.send_response(500 if refuse else 200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/desc.xml"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
