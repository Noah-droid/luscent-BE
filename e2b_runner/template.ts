import { Template } from 'e2b'

export const template = Template()
  .fromImage('mcr.microsoft.com/playwright/python:v1.58.0-jammy')
  .setUser('root')
  .setWorkdir('/')
  .setEnvs({
    'PLAYWRIGHT_BROWSERS_PATH': '/ms-playwright',
  })
  .setEnvs({
    'DISPLAY': ':1',
  })
  .runCmd('apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y xvfb fluxbox x11vnc novnc websockify && rm -rf /var/lib/apt/lists/*')
  .runCmd('pip install --no-cache-dir playwright requests locust pytest beautifulsoup4 python-dotenv jsonpath-ng')
  .runCmd('playwright install --with-deps chromium')
  .setWorkdir('/home/user')
  .copy('locustfile.py', '/home/user/locustfile.py')
  .setEnvs({
    'PYTHONDONTWRITEBYTECODE': '1',
  })
  .setEnvs({
    'PYTHONUNBUFFERED': '1',
  })
  .setUser('user')