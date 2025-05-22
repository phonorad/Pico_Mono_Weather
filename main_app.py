import network
import urequests
import time
import machine
import math
import ntptime
import gc
import framebuf
import uio
import sys
from phew import access_point, connect_to_wifi, is_connected_to_wifi, dns, server
from phew.template import render_template
from phew import logging
from phew.server import Response
import ujson as json
import os
import _thread
import socket # temporary for troubleshooting

# === Software Version ===
__version__ = "1.1.0"
# ========================

# === Definitons for Wifi Setup and Access ===
AP_NAME = "pico weather"
AP_DOMAIN = "picoweather.net"
AP_TEMPLATE_PATH = "ap_templates"
APP_TEMPLATE_PATH = "app_templates"
SETTINGS_FILE = "settings.json"
WIFI_MAX_ATTEMPTS = 3

# Comment out the display driver not used
from ssd1306 import SSD1306_I2C  # for 0.96" 128x64 Mono Oled
#from sh1106 import SH1106_I2C   # for 1.3" 128x64 Mono Oled
# ===================================
from machine import I2C, Pin

# === Initialize/define parameters ===
SYNC_INTERVAL = 3600 # Sync to NTP time server every hour
WEATH_INTERVAL = 300 # Update weather every 5 mins
last_sync = 0
last_weather_update = 0
start_update_requested = False
continue_requested = False
UPLOAD_TEMP_SUFFIX = ".tmp"

# === Define Months ===
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# === Define timezone ===
UTC_OFFSET = -4 * 3600  # For EDT (UTC-4), or -5*3600 for EST (UTC-5)

# === OLED Setup ===
WIDTH = 128
HEIGHT = 64
i2c = I2C(0, scl=Pin(1), sda=Pin(0))

# === Other GPIO Setup ===
onboard_led = machine.Pin("LED", machine.Pin.OUT)
setup_sw = machine.Pin(5, machine.Pin.IN, machine.Pin.PULL_UP)

# Comment out the display driver not used
oled = SSD1306_I2C(WIDTH, HEIGHT, i2c)
#oled = SH1106_I2C(WIDTH, HEIGHT, i2c)

# === AP and Wi-Fi Setup ===
def machine_reset():
    time.sleep(2)
    print("Resetting...")
    machine.reset()

def setup_mode():
    print("Entering setup mode...")
    oled.fill(0)
    center_text("Setup Mode", 5)
    center_text("Open Browser", 20)
    center_text("Select Wifi", 35)
    center_text("'Pico Weather'", 50)
    oled.show()

    def ap_index(request):
        if request.headers.get("host").lower() != AP_DOMAIN.lower():
            return render_template(f"{AP_TEMPLATE_PATH}/redirect.html", domain = AP_DOMAIN.lower())

        return render_template(f"{AP_TEMPLATE_PATH}/index_wifi_zip.html")

    def ap_configure(request):
        print("Saving wifi and zip credentials...")

        with open(SETTINGS_FILE, "w") as f:
            json.dump(request.form, f)
            f.close()

        # Reboot from new thread after we have responded to the user.
        _thread.start_new_thread(machine_reset, ())
        return render_template(f"{AP_TEMPLATE_PATH}/configured.html", ssid = request.form["ssid"])
        
    def ap_catch_all(request):
        if request.headers.get("host") != AP_DOMAIN:
            return render_template(f"{AP_TEMPLATE_PATH}/redirect.html", domain = AP_DOMAIN)

        return "Not found.", 404

    server.add_route("/", handler = ap_index, methods = ["GET"])
    server.add_route("/configure", handler = ap_configure, methods = ["POST"])
    server.set_callback(ap_catch_all)

    ap = access_point(AP_NAME)
    ip = ap.ifconfig()[0]
    dns.run_catchall(ip)

def start_update_mode():
    print("starting update mode")
    ip = network.WLAN(network.STA_IF).ifconfig()[0]
    print(f"start_update_mode: got IP = {ip}")
    
    oled.fill(0)
    center_text("SW update mode", 0)
    center_text("Enter", 10)
    center_text("http://", 20)
    center_text(f"{ip}", 30)
    center_text("/swup", 40)
    center_text("into browser", 50)
    oled.show()

    def ap_version(request):
        # Return the version defined in main.py
        return Response(__version__, status=200, headers={"Content-Type": "text/plain"})

    def swup_handler(request):
        # Serve your software update HTML page here
        return render_template(f"{AP_TEMPLATE_PATH}/index_swup_git.html")
    
    def favicon_handler(request):
        return Response("", status=204)  # No Content

    def continue_handler(request):
        global continue_requested
        continue_requested = True
        print("Continue requested, restarting device...")
        # Schedule reboot after response is sent
        # Start a delayed reset thread to allow HTTP response to complete
        
        def delayed_restart():
            time.sleep(1)  # Wait ~1s to let HTTP response flush
            machine_reset()

        _thread.start_new_thread(machine_reset, ())
        return Response("Restarting device...", status=200, headers={"Content-Type": "text/plain"})

    def upload_handler(request):
        print("Upload handler triggered")
        try:
            data = json.loads(request.data.decode("utf-8"))
            filename = data.get("filename")
            content = data.get("content")

            if not filename or content is None:
                return Response("Missing filename or content", status=400)
            with open(filename, "w") as f:
                f.write(content)

            print(f"Uploaded file: {filename}")
            return Response(f"Saved {filename}", status=200)
        except Exception as e:
            print(f"Upload error: {e}")
            return Response(f"Error: {e}", status=500)
        
    def catch_all_handler(request):
        print(f"Fallback route hit: {request.method} {request.path}")
        return Response("Route not found", status=404)
        
    server.add_route("/swup", handler=swup_handler, methods=["GET"])
    server.add_route("/version", handler=ap_version, methods=["GET"])
    server.add_route("/favicon.ico", handler=favicon_handler, methods=["GET"])
    server.add_route("/continue", handler=continue_handler, methods=["POST"])
    server.add_route("/upload", handler=upload_handler, methods=["POST"])
        
    # Start the server (if not already running)
    print(f"Waiting for user at http://{ip}/swup ...")
    server.run()

    # Wait until user clicks OK
    while not continue_requested:
        time.sleep(0.1)

# === Handler for button presses during operation ===
def setup_sw_handler(pin):
    global press_time, long_press_triggered, start_update_requested
    if pin.value() == 0:  # Falling edge: button pressed
        press_time = time.ticks_ms()
        long_press_triggered = False
    else:  # Rising edge: button released
        if press_time is not None:
            duration = time.ticks_diff(time.ticks_ms(), press_time)
            if duration >= 5000:  # 5 seconds
                long_press_triggered = True
                print("Long press detected!")
                # Set flag for main loop to poll and to call start_update_mode
                start_update_requested = True
            press_time = None
# Set up input as irq triggered, falling edge            
setup_sw.irq(trigger=machine.Pin.IRQ_FALLING | machine.Pin.IRQ_RISING, handler=setup_sw_handler)
    
# === Set correct time from NTP server ===
def sync_time():
    try:
        print("Syncing time with NTP server...")
        ntptime.settime()  # This sets the RTC from the network time
        print("Time synced successfully!")
    except Exception as e:
        print(f"Failed to sync time: {e}")
        
def is_daytime():
    t = time.localtime()
    hour = t[3]  # Hour is the 4th element in the tuple
    return 7 <= hour < 19  # Define day as between 7am and 7pm (0700 to 1900)
        
# === Calculate correct local time ===
def localtime_with_offset():
    t = time.mktime(time.localtime())  # seconds since epoch UTC
    t += UTC_OFFSET                    # add offset seconds
    return time.localtime(t)

# === Determine latitude and longitude from zip code ===
def get_lat_lon(zip_code, country_code="us"):
    url = f"http://api.zippopotam.us/{country_code}/{zip_code}"
    try:
        response = urequests.get(url)
        if response.status_code == 200:
            data = response.json()
            place = data["places"][0]
            lat = float(place["latitude"])
            lon = float(place["longitude"])
            return lat, lon
        else:
            print("API response error:", response.status_code)
    except Exception as e:
        print("Failed to get lat/lon:", e)
    return None, None

# === Weather Setup ===
#LAT = 41.4815
#LON = -73.2132
USER_AGENT = "PicoWeatherDisplay (contact@example.com)"  # replace with your info

def get_weather_data(lat, lon):
    try:
        headers = {"User-Agent": USER_AGENT}

        # Step 1: Get forecast and observation stations
        r = urequests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=headers)
        point_data = r.json()
        r.close()
        gc.collect()

        forecast_url = point_data["properties"]["forecast"]
        obs_station_url = point_data["properties"]["observationStations"]
        del point_data  # free memory
        gc.collect()

        # Step 2: Get observation stations list
        r = urequests.get(obs_station_url, headers=headers)
        stations_data = r.json()
        r.close()
        gc.collect()

        features = stations_data.get("features", [])
        if not features:
            return None
        station_id = features[0]["properties"]["stationIdentifier"]
        del stations_data, features  # free memory
        gc.collect()

        # Step 3: Get latest observations
        obs_url = f"https://api.weather.gov/stations/{station_id}/observations/latest"
        r = urequests.get(obs_url, headers=headers)
        obs_data = r.json()
        r.close()
        gc.collect()

        obs = obs_data.get("properties", {})
        del obs_data
        gc.collect()

        temp_c = obs.get("temperature", {}).get("value", None)
        humidity = obs.get("relativeHumidity", {}).get("value", None)

        if temp_c is not None:
            temp_f = round(temp_c * 9 / 5 + 32)
        else:
            temp_f = None

        # Get forecast data
        r = urequests.get(forecast_url, headers=headers)
        forecast_data = r.json()
        r.close
        gc.collect()
        
        forecast = "N/A"
        periods = forecast_data.get("properties", {}).get("periods", {})
        if periods:
            forecast = periods[0].get("shortForecast", "N/A")
            
        return temp_f, humidity, forecast

    except Exception as e:
        print("Error:", e)
        return None

# === Display Helpers ===

def center_text(text, y):
    x = (128 - len(text) * 8) // 2  # 8 px approx per char
    oled.text(text, x, y)
    
def center_text_under_icon(text, icon_x, icon_width=32, char_width=8):
    text_pixel_width = len(text) * char_width
    icon_center_x = icon_x + icon_width // 2
    return max(icon_center_x - text_pixel_width // 2, 0)

def simplify_forecast(forecast):
    KEYWORDS = [
        "Thunderstorms", "T-storms", "Tstorms",
        "Sunny", "Cloudy", "Rain", "Showers", "Fog",
        "Snow", "Clear", "Wind", "Drizzle", "Storm", "Sleet", "Haze",
        "Partly Sunny", "Mostly Sunny", "Partly Cloudy", "Mostly Cloudy",
        "Slight Chance", "Chance"
    ]
    ABBREVIATIONS = {
        "Thunderstorms": "Tstorms",
        "T-storms": "Tstorms",
        "Tstorms": "Tstorms",
        "Partly Sunny": "P Sunny",
        "Mostly Sunny": "M Sunny",
        "Partly Cloudy": "P Cloudy",
        "Mostly Cloudy": "M Cloudy",
        "Slight Chance": "Chc",
        "Chance": "Chc"
    }

    found = []
    lower_forecast = forecast.lower()
    for word in KEYWORDS:
        if word.lower() in lower_forecast:
            normalized = ABBREVIATIONS.get(word, word)
            if normalized not in found:
                found.append(normalized)

    if found:
        return found[:2]
    else:
        # Fallback: truncate original forecast to 8 characters
        return [forecast[:8]]

# Weather icon byte arrays
# 'clear_night', 32x32x
clear_night_data = bytearray([
    0x00, 0x1f, 0xe0, 0x00, 0x00, 0x1f, 0xe0, 0x00, 0x00, 0x60, 0x1e, 0x00, 0x00, 0x60, 0x1f, 0x00,
    0x01, 0x9f, 0x01, 0x80, 0x03, 0x9f, 0x80, 0xc0, 0x03, 0xff, 0xc0, 0x60, 0x03, 0xe0, 0x60, 0x30,
    0x03, 0xc0, 0x20, 0x30, 0x03, 0x80, 0x18, 0x30, 0x01, 0x00, 0x18, 0x30, 0x00, 0x00, 0x18, 0x0c,
    0x00, 0x00, 0x18, 0x0c, 0x00, 0x00, 0x18, 0x0c, 0x00, 0x00, 0x18, 0x0c, 0x00, 0x00, 0x67, 0x8c,
    0x00, 0x00, 0x63, 0x8c, 0x00, 0x01, 0x80, 0x0c, 0x00, 0x01, 0x80, 0x0c, 0x00, 0x01, 0xe0, 0x0c,
    0x00, 0x01, 0xe0, 0x0c, 0xe0, 0x03, 0xc0, 0x10, 0xf0, 0x07, 0x80, 0x30, 0xff, 0xff, 0x00, 0x30,
    0xcf, 0xf8, 0x00, 0x30, 0xcf, 0xf0, 0x00, 0x20, 0x30, 0x00, 0x00, 0xc0, 0x10, 0x00, 0x01, 0x80,
    0x0f, 0x00, 0x1f, 0x00, 0x07, 0x80, 0x1e, 0x00, 0x00, 0x7f, 0xe0, 0x00, 0x00, 0x7f, 0xe0, 0x00
]) 
# 'clear day', 32x32px
clear_day_data = bytearray([
    0x00, 0x01, 0x80, 0x00, 0x00, 0x01, 0x80, 0x00, 0x00, 0x07, 0xe0, 0x00, 0x00, 0x07, 0xe0, 0x00,
    0x07, 0x00, 0x00, 0xe0, 0x0f, 0x80, 0x00, 0xf0, 0x0f, 0x03, 0xc0, 0x70, 0x0e, 0x07, 0xe0, 0x30,
    0x04, 0x0f, 0xf0, 0x00, 0x00, 0x7f, 0xfe, 0x00, 0x00, 0x7f, 0xff, 0x00, 0x00, 0x7f, 0xff, 0x00,
    0x00, 0xff, 0xff, 0x00, 0x31, 0xe7, 0xe7, 0x8c, 0x33, 0xe7, 0xf7, 0xcc, 0xf3, 0xff, 0xff, 0xcf,
    0xf3, 0xff, 0xff, 0xcf, 0x33, 0xe0, 0x07, 0xcc, 0x31, 0xe0, 0x07, 0x8c, 0x00, 0xf0, 0x0f, 0x00,
    0x00, 0x78, 0x1f, 0x00, 0x00, 0x7f, 0xff, 0x00, 0x00, 0x7f, 0xff, 0x00, 0x00, 0x3f, 0xfe, 0x00,
    0x0c, 0x07, 0xe0, 0x30, 0x0e, 0x03, 0xc0, 0x70, 0x0f, 0x00, 0x00, 0xf0, 0x07, 0x00, 0x00, 0xe0,
    0x00, 0x07, 0xe0, 0x00, 0x00, 0x07, 0xe0, 0x00, 0x00, 0x01, 0x80, 0x00, 0x00, 0x01, 0x80, 0x00
]) 
# 'part_cloudy_day', 32x32px
part_cloudy_day_data = bytearray([
    0x00, 0x01, 0x80, 0x00, 0x00, 0x01, 0x80, 0x00, 0x00, 0x07, 0xe0, 0x00, 0x00, 0x07, 0xe0, 0x00,
    0x07, 0x00, 0x00, 0xe0, 0x0f, 0x80, 0x00, 0xf0, 0x0f, 0x03, 0xc0, 0x70, 0x0e, 0x07, 0xe0, 0x30,
    0x04, 0x0f, 0xf0, 0x00, 0x00, 0x7f, 0xfe, 0x00, 0x00, 0x7f, 0xff, 0x00, 0x00, 0x7f, 0xff, 0x00,
    0x00, 0xff, 0xff, 0x00, 0x31, 0xff, 0xff, 0x8c, 0x33, 0xff, 0xff, 0xcc, 0xf3, 0xfe, 0x07, 0xcf,
    0xf3, 0xfe, 0x03, 0xcf, 0x33, 0xf8, 0x01, 0xcc, 0x31, 0xf8, 0x01, 0xcc, 0x00, 0xf8, 0x01, 0xf0,
    0x00, 0x78, 0x01, 0xf0, 0x01, 0x80, 0x01, 0xe8, 0x03, 0x80, 0x01, 0xcc, 0x07, 0x00, 0x00, 0x07,
    0x0e, 0x00, 0x00, 0x03, 0x0e, 0x00, 0x00, 0x03, 0x0e, 0x00, 0x00, 0x03, 0x06, 0x00, 0x00, 0x03,
    0x03, 0x00, 0x00, 0x0c, 0x01, 0x80, 0x00, 0x08, 0x00, 0x7f, 0xff, 0xf0, 0x00, 0x7f, 0xff, 0xf0,
]) 
# 'part_cloudy_night', 32x32px
part_cloudy_night_data = bytearray([
    0x00, 0x1f, 0xe0, 0x00, 0x00, 0x1f, 0xe0, 0x00, 0x00, 0x60, 0x1e, 0x00, 0x00, 0x60, 0x1f, 0x00,
    0x01, 0x9f, 0x01, 0x80, 0x03, 0x9f, 0x80, 0xc0, 0x03, 0xff, 0xc0, 0x60, 0x03, 0xe0, 0x60, 0x30,
    0x03, 0xc0, 0x20, 0x30, 0x03, 0x80, 0x18, 0x30, 0x01, 0x00, 0x18, 0x30, 0x00, 0x00, 0x18, 0x0c,
    0x00, 0x00, 0x18, 0x0c, 0x00, 0x01, 0xf8, 0x0c, 0x00, 0x01, 0xf8, 0x0c, 0x00, 0x06, 0x06, 0x0c,
    0x00, 0x06, 0x03, 0x0c, 0x00, 0x18, 0x01, 0x8c, 0x00, 0x18, 0x01, 0xcc, 0x00, 0x78, 0x01, 0xfc,
    0x00, 0x78, 0x01, 0xfc, 0xe1, 0x80, 0x01, 0xfc, 0xf3, 0x80, 0x01, 0xcc, 0xff, 0x00, 0x00, 0x07,
    0xce, 0x00, 0x00, 0x03, 0xde, 0x00, 0x00, 0x03, 0x3e, 0x00, 0x00, 0x03, 0x1e, 0x00, 0x00, 0x03,
    0x0f, 0x00, 0x00, 0x0c, 0x07, 0x80, 0x00, 0x08, 0x00, 0x7f, 0xff, 0xf0, 0x00, 0x7f, 0xff, 0xf0
])
# 'cloudy', 32x32px
cloudy_data = bytearray([
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x03, 0xc0, 0x00, 0x00, 0x07, 0xe0, 0x00, 0x00, 0x0f, 0xf0, 0x00, 0x00, 0x18, 0x18, 0x00,
    0x00, 0x10, 0x08, 0x00, 0x00, 0x60, 0x06, 0x00, 0x00, 0xe0, 0x07, 0x00, 0x01, 0xe0, 0x07, 0x80,
    0x03, 0xc0, 0x07, 0xc0, 0x06, 0x00, 0x07, 0x30, 0x0c, 0x00, 0x02, 0x30, 0x10, 0x00, 0x00, 0x0c,
    0x30, 0x00, 0x00, 0x0c, 0x30, 0x00, 0x00, 0x0c, 0x30, 0x00, 0x00, 0x0c, 0x0c, 0x00, 0x00, 0x30,
    0x0e, 0x00, 0x00, 0x30, 0x03, 0xff, 0xff, 0xc0, 0x03, 0xff, 0xff, 0xc0, 0x01, 0xff, 0xff, 0x80,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
]) 
# 'rain', 32x32px
rain_data = bytearray([
    0x00, 0x07, 0xe0, 0x00, 0x00, 0x07, 0xe0, 0x00, 0x00, 0x18, 0x18, 0x00, 0x00, 0x18, 0x18, 0x00,
    0x00, 0x60, 0x06, 0x00, 0x00, 0x60, 0x07, 0x00, 0x01, 0xe0, 0x07, 0x80, 0x03, 0xe0, 0x07, 0xc0,
    0x03, 0xc0, 0x07, 0xc0, 0x0e, 0x00, 0x07, 0x30, 0x0c, 0x00, 0x02, 0x30, 0x30, 0x00, 0x00, 0x0c,
    0x30, 0x00, 0x00, 0x0c, 0x30, 0x00, 0x00, 0x0c, 0x10, 0x00, 0x00, 0x0c, 0x0c, 0x00, 0x00, 0x30,
    0x06, 0x00, 0x00, 0x30, 0x03, 0xff, 0xff, 0xc0, 0x01, 0xff, 0xff, 0x80, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x08, 0x80, 0x03, 0x99, 0x99, 0xc0, 0x03, 0x99, 0x99, 0xc0,
    0x03, 0x99, 0x99, 0xc0, 0x03, 0x99, 0x99, 0xc0, 0x03, 0x99, 0x99, 0xc0, 0x03, 0x91, 0x89, 0xc0,
    0x03, 0x81, 0x81, 0xc0, 0x01, 0x01, 0x80, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
])
# 'tstorm', 32x32px
tstorm_data = bytearray([
    0x00, 0x07, 0xe0, 0x00, 0x00, 0x07, 0xe0, 0x00, 0x00, 0x18, 0x18, 0x00, 0x00, 0x18, 0x18, 0x00,
    0x00, 0x60, 0x06, 0x00, 0x00, 0x60, 0x07, 0x00, 0x01, 0xe0, 0x07, 0x80, 0x03, 0xe0, 0x07, 0xc0,
    0x03, 0xc0, 0x07, 0xc0, 0x0e, 0x00, 0x07, 0x30, 0x0c, 0x00, 0x02, 0x30, 0x30, 0x00, 0x00, 0x0c,
    0x30, 0x00, 0x00, 0x0c, 0x30, 0x00, 0x00, 0x0c, 0x10, 0x00, 0x00, 0x0c, 0x0c, 0x00, 0x00, 0x30,
    0x06, 0x00, 0x00, 0x30, 0x03, 0xff, 0xff, 0xc0, 0x01, 0xff, 0xff, 0x80, 0x00, 0x38, 0x07, 0x00,
    0x00, 0x18, 0x06, 0x00, 0x00, 0x60, 0x08, 0x00, 0x00, 0x60, 0x18, 0x00, 0x01, 0xff, 0x0e, 0x00,
    0x01, 0xff, 0x86, 0x00, 0x01, 0xff, 0x06, 0x00, 0x00, 0x06, 0x18, 0x00, 0x00, 0x04, 0x08, 0x00,
    0x00, 0x18, 0x00, 0x00, 0x00, 0x10, 0x00, 0x00, 0x00, 0x60, 0x00, 0x00, 0x00, 0x60, 0x00, 0x00
])
# 'snow', 32x32px
snow_data = bytearray([
    0x00, 0x07, 0xe0, 0x00, 0x00, 0x07, 0xe0, 0x00, 0x00, 0x18, 0x18, 0x00, 0x00, 0x18, 0x18, 0x00,
    0x00, 0x60, 0x06, 0x00, 0x00, 0x60, 0x07, 0x00, 0x01, 0xe0, 0x07, 0x80, 0x03, 0xe0, 0x07, 0xc0,
    0x03, 0xc0, 0x07, 0xc0, 0x0e, 0x00, 0x07, 0x30, 0x0c, 0x00, 0x02, 0x30, 0x30, 0x00, 0x00, 0x0c,
    0x30, 0x00, 0x00, 0x0c, 0x30, 0x00, 0x00, 0x0c, 0x10, 0x00, 0x00, 0x0c, 0x0c, 0x00, 0x00, 0x30,
    0x06, 0x00, 0x00, 0x30, 0x03, 0xff, 0xff, 0xc0, 0x01, 0xff, 0xff, 0x80, 0x00, 0x38, 0x00, 0x00,
    0x00, 0x18, 0x00, 0x00, 0x00, 0x64, 0x00, 0x80, 0x00, 0x66, 0x01, 0xc0, 0x00, 0x3c, 0x03, 0xe0,
    0x0c, 0x18, 0x07, 0x30, 0x0e, 0x00, 0x03, 0x20, 0x33, 0x00, 0x61, 0xc0, 0x13, 0x00, 0x60, 0x80,
    0x0e, 0x01, 0x98, 0x00, 0x04, 0x01, 0x98, 0x00, 0x00, 0x00, 0x60, 0x00, 0x00, 0x00, 0x60, 0x00
])
# 'fog', 32x32px
fog_data = bytearray([
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1c, 0x00, 0xe0, 0x0f, 0x3e, 0x01, 0xe0, 0x0f,
    0xc3, 0x02, 0x18, 0x10, 0xc1, 0x86, 0x18, 0x30, 0x81, 0xfc, 0x0f, 0xe0, 0x00, 0x78, 0x07, 0xc0,
    0x00, 0x70, 0x03, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0xf0, 0x07, 0x80, 0x3c, 0xf0, 0x07, 0x80, 0x3c, 0x0c, 0x18, 0x60, 0xc3,
    0x06, 0x18, 0x61, 0x83, 0x03, 0xe0, 0x1f, 0x00, 0x01, 0xe0, 0x1e, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80, 0x0c, 0x00, 0x60,
    0xc0, 0x1e, 0x00, 0xf0, 0xc0, 0x1e, 0x01, 0xf0, 0x30, 0x61, 0x83, 0x0c, 0x10, 0x40, 0x86, 0x0c,
    0x0f, 0x80, 0x78, 0x03, 0x07, 0x00, 0x38, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
])
    
def draw_weather_icon(forecast, x, y):
    oled.fill_rect(x, y, 48, 32, 0)
    f = forecast.lower()
    day = is_daytime()

#    print("Forecast text:", f)  # Uncomment to debug forecast test string and icon display

    if "sun" in f or "clear" in f:
        if day:
            icon_data = clear_day_data
        else:
            icon_data = clear_night_data
        icon = framebuf.FrameBuffer(icon_data, 32, 32, framebuf.MONO_HLSB)
        oled.blit(icon, x, y)
    elif "partly cloudy" in f or "partly cloud" in f:
        if day:
            icon_data = partly_cloudy_day_data
        else:
            icon_data = party_cloudy_night_data
        icon = framebuf.FrameBuffer(icon_data, 32, 32, framebuf.MONO_HLSB)
        oled.blit(icon, x, y)
    elif "thunderstorm" in f or "thunderstorms" in f or "t-storm" in f:
        tstorm_icon = framebuf.FrameBuffer(tstorm_data, 32, 32, framebuf.MONO_HLSB)
        oled.blit(tstorm_icon, x, y)
    elif "cloud" in f or "overcast" in f:
        cloudy_icon = framebuf.FrameBuffer(cloudy_data, 32, 32, framebuf.MONO_HLSB)
        oled.blit(cloudy_icon, x, y)
    elif "rain" in f or "showers" in f:
        rain_icon = framebuf.FrameBuffer(rain_data, 32, 32, framebuf.MONO_HLSB)
        oled.blit(rain_icon, x, y)
    elif "fog" in f or "haze" in f:
        fog_icon = framebuf.FrameBuffer(fog_data, 32, 32, framebuf.MONO_HLSB)
        oled.blit(fog_icon, x, y)
    elif "snow" in f or "flurries" in f:
        snow_icon = framebuf.FrameBuffer(snow_data, 32, 32, framebuf.MONO_HLSB)
        oled.blit(snow_icon, x, y)
    else:
        print("Unknown forecast, defaulting to sun:", forecast)
        icon_data = clear_day_data  # Display sun if there is no match of keywords to icons
        icon = framebuf.FrameBuffer(icon_data, 32, 32, framebuf.MONO_HLSB)
        oled.blit(icon, x, y)

def display_weather(temp, humidity, forecast):
    oled.fill(0)
    now = localtime_with_offset()
    date_str = "{} {:02d}, {:04d}".format(MONTHS[now[1]-1], now[2], now[0])
    time_str = format_12h_time(now)

    center_text(date_str, 0)
    center_text(time_str, 10)

    oled.text(f"{temp}F", 20, 30)
    oled.text(f"{int(humidity)}%", 20, 45)

    draw_weather_icon(forecast, x=70, y=18)
        
    ICON_X = 70  # wherever your icon is drawn
    ICON_WIDTH = 32

    lines = simplify_forecast(forecast)

    if len(lines) == 2:
        oled.text(lines[0], center_text_under_icon(lines[0], ICON_X, ICON_WIDTH), 49)
        oled.text(lines[1], center_text_under_icon(lines[1], ICON_X, ICON_WIDTH), 57)
    else:
        oled.text(lines[0], center_text_under_icon(lines[0], ICON_X, ICON_WIDTH), 55)
    
    oled.show()
    
def format_12h_time(t):
    hour = t[3]
    am_pm = "AM"
    if hour == 0:
        hour_12 = 12
    elif hour > 12:
        hour_12 = hour - 12
        am_pm = "PM"
    elif hour == 12:
        hour_12 = 12
        am_pm = "PM"
    else:
        hour_12 = hour
    return "{:2d}:{:02d}:{:02d} {}".format(hour_12, t[4], t[5], am_pm)    

# === Helpers for Wifi /AP Portion of App - Add back in if/when needed =====
# def application_mode():
#    print("Entering application mode.")
#    onboard_led = machine.Pin("LED", machine.Pin.OUT)

#    def app_index(request):
#        return render_template(f"{APP_TEMPLATE_PATH}/index.html")

#    def app_toggle_led(request):
#        onboard_led.toggle()
#        return "OK"
    
#    def app_get_temperature(request):
        # Not particularly reliable but uses built in hardware.
        # Demos how to incorporate senasor data into this application.
        # The front end polls this route and displays the output.
        # Replace code here with something else for a 'real' sensor.
        # Algorithm used here is from:
        # https://www.coderdojotc.org/micropython/advanced-labs/03-internal-temperature/
#        sensor_temp = machine.ADC(4)
#        reading = sensor_temp.read_u16() * (3.3 / (65535))
#        temperature = 27 - (reading - 0.706)/0.001721
#        return f"{round(temperature, 1)}"
    
#    def app_reset(request):
        # Deleting the WIFI configuration file will cause the device to reboot as
        # the access point and request new configuration.
#        os.remove(WIFI_FILE)
        # Reboot from new thread after we have responded to the user.
#        _thread.start_new_thread(machine_reset, ())
#        return render_template(f"{APP_TEMPLATE_PATH}/reset.html", access_point_ssid = AP_NAME)

#    def app_catch_all(request):
#        return "Not found.", 404

#    server.add_route("/", handler = app_index, methods = ["GET"])
#    server.add_route("/toggle", handler = app_toggle_led, methods = ["GET"])
#    server.add_route("/temperature", handler = app_get_temperature, methods = ["GET"])
#    server.add_route("/reset", handler = app_reset, methods = ["GET"])
    # Add other routes for your application...
#    server.set_callback(app_catch_all)

# === Weather Program ===
def application_mode(zip_code):
    print("Entering application mode.")
    global start_update_requested
#    onboard_led = machine.Pin("LED", machine.Pin.OUT)
#    setup_wifi_sw = machine.Pin(5, machine.Pin.IN)


    # Initial time sync
    sync_time()
    last_sync = time.time()
    
    # Determine Latitude and Longitude
    lat, lon = get_lat_lon(zip_code)
    print("Latitude:", lat)
    print("Longitude:", lon)

    # Initial weather fetch
    data = get_weather_data(lat, lon)
    if data:
        temp, humidity, forecast = data
        print(f"Temp: {temp}F, Humidity: {humidity}%, Forecast: {forecast}")
    else:
        temp, humidity, forecast = None, None, None

    last_weather_update = time.time()

    while True:
        if start_update_requested:
            start_update_requested = False
            print("going to start update mode")
            start_update_mode()
            return   # exit application mode, switching to update mode
        # Update time display every second
        oled.fill(0)

        if temp is not None:
            display_weather(temp, humidity, forecast)
        else:
            oled.text("Weather data", 0, 20)
            oled.text("unavailable", 0, 30)

        # Draw time (you may want to center it, etc.)
        now = localtime_with_offset()
        date_str = "{} {:02d}, {:04d}".format(MONTHS[now[1]-1], now[2], now[0])
        time_str = format_12h_time(now)

        center_text(date_str, 0)
        center_text(time_str, 10)

        oled.show()

        current_time = time.time()
    
        # Sync time every SYNC_INTERVAL (1 hour/3600 sec)
        if current_time - last_sync >= SYNC_INTERVAL:
            sync_time()
            last_sync = current_time
    
        # Refresh weather WEATH_INTERVAL (5 min/300 sec) 
        if time.time() - last_weather_update >= WEATH_INTERVAL:
            new_data = get_weather_data(lat, lon)
            if new_data:
                temp, humidity, forecast = new_data
                print(f"Updated: Temp: {temp}F, Humidity: {humidity}%, Forecast: {forecast}")
            else:
                temp, humidity, forecast = None, None, None
            last_weather_update = time.time()

        time.sleep(1)
    
# === Main Program - Connnect to Wifi or goto AP mode Wifi setup ===
# ===                If Wifi connection OK, go to Weather program ===
# Figure out which mode to start up in...
try:
#    onboard_led = machine.Pin("LED", machine.Pin.OUT)
#    setup_wifi_sw = machine.Pin(5, machine.Pin.IN, machine.Pin.PULL_UP)
    os.stat(SETTINGS_FILE)
    # File was found, attempt to connect to wifi...
    # See if setup wifi switch is pressed
    if setup_sw.value() == False:
        t = 50  # Switch must be pressed for 5 seconds to reset wifi config
        while setup_sw.value() == False and t > 0:
            t -= 1
            time.sleep(0.1)
        if setup_sw.value() == False:
            print("Setup switch ")
            os.remove(SETTINGS_FILE)
            machine_reset()
    
    with open(SETTINGS_FILE) as f:
        wifi_current_attempt = 1
        settings = json.load(f)
        while (wifi_current_attempt < WIFI_MAX_ATTEMPTS):
            print(settings['ssid'])
            print(settings['password'])
            print(settings['zip'])
            ip_address = connect_to_wifi(settings["ssid"], settings["password"])
            zip_code = settings["zip"]
            if is_connected_to_wifi():
                print(f"Connected to wifi, IP address {ip_address}")
                center_text(f"v{__version__}", 5)
                center_text("Connect to:", 20)
                center_text(f"{settings['ssid']}", 35)
                center_text(f"{ip_address}", 50)
                oled.show()
                time.sleep(2)
                break
            else:
                wifi_current_attempt += 1
                
        if is_connected_to_wifi():
            application_mode(zip_code)
        else:
            # Bad configuration, delete the credentials file, reboot
            # into setup mode to get new credentials from the user.
            print("Bad wifi connection!")
            os.remove(SETTINGS_FILE)
            machine_reset()

except Exception as e:
    # Either no wifi configuration file found, or something went wrong, 
    # so go into setup mode.
    
    # Send exception info to console
    print("Exception occurred:", e)
    
    logging.error("Exception occurred: {}".format(e))

    # Capture traceback into a string and log it
    buf = uio.StringIO()
    sys.print_exception(e, buf)
    logging.exception(buf.getvalue())
    
    setup_mode()
    server.run()

#Start the web server...
#server.run()    
