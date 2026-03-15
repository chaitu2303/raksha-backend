
from datetime import datetime

class MockAlert:
    def __init__(self):
        self.alertId = "test-123"
        self.alertType = "MISSING_PERSON"
        self.title = "Child Missing near Park"
        self.description = "Blue shirt, 5 years old, seen near the main gate. Please help us find him."
        self.location = "City Park, Zone 4"
        self.latitude = 17.7215
        self.longitude = 83.3046
        self.personName = "Rahul"
        self.age = "5"
        self.gender = "Male"
        self.contactNumber = "+91 9876543210"
        self.imageUrl = "https://raksha-alert.vercel.app/images/rahul.jpg"

def generate_professional_template(alert) -> str:
    now = datetime.now()
    timestamp_str = now.strftime("%d %B %Y | %I:%M %p")
    incident_label = alert.alertType.replace('_', ' ').title()
    map_link = f"https://maps.google.com/?q={alert.latitude},{alert.longitude}"
    
    message = f"🚨 URGENT COMMUNITY ALERT 🚨\nRAKSHA SAFETY SYSTEM\n\n"
    message += f"⚠️ INCIDENT TYPE\n{incident_label}: {alert.title}\n\n"
    message += f"👤 PERSON DETAILS\nName: {alert.personName}\nAge: {alert.age}\nGender: {alert.gender}\n\n"
    message += f"📍 LAST SEEN LOCATION\n{map_link}\n\n"
    message += f"🕒 DATE & TIME\n{timestamp_str}\n\n"
    message += f"📝 DESCRIPTION\n{alert.description}\n\n"
    message += f"📷 PHOTO\n{alert.imageUrl}\n\n"
    message += f"📞 EMERGENCY CONTACT\n{alert.contactNumber}\n\n"
    message += f"⚠️ If anyone has information, please respond immediately.\n\n"
    message += f"— Raksha Emergency Response System"
    return message

if __name__ == "__main__":
    alert = MockAlert()
    msg = generate_professional_template(alert)
    print(f"--- Template Output ---\n{msg}\n---")
    print(f"Message Length: {len(msg)} characters")
