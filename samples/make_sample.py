"""Build the sample workbooks in the implementation team's template layout.

- Automation_Email.xlsx : TS_EML001 against the built-in Acme Mail demo app
- Outlook_Email.xlsx    : TS_OUT001 sends a real email with Outlook on the web
"""
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))  # run as a script from any folder

from app.excel_io import build_workbook, parse_test_data  # noqa: E402

ACME = dict(
    path=HERE / "Automation_Email.xlsx", case_id="TS_EML001", role="User",
    title="Automation email: create, write and send an email",
    description="Log in to Acme Mail, compose an email to the finance team, send it and "
                "confirm it appears in Sent",
    heading="Automation email (create, write and send)",
    steps=[
        ("Login to Acme Mail",
         "1. Open a browser and go to the Acme Mail login page\n2. Input User Name & Password\n3. Click Sign in",
         "User Name: demo.user\nPassword: demo123", "User is logged in and the Inbox is displayed"),
        ("Open a new message", "Navigation:\nInbox\nSteps:\n1. Click Compose", "", "New message form opens"),
        ("Write the email",
         "Navigation:\nContinue from previous step\nSteps:\n1. Enter the recipient in To\n"
         "2. Enter the Subject\n3. Enter the message body",
         "To: finance.team@acme.test\nSubject: Q3 inventory adjustment summary\n"
         "Body: Hi team, the Q3 inventory adjustments have been posted. Regards, Demo User",
         "To, Subject and Message are populated"),
        ("Send the email", "Navigation:\nContinue from previous step\nSteps:\n1. Click Send", "",
         "Message sent confirmation is displayed"),
        ("Verify the email in Sent",
         "Navigation:\nSent folder\nSteps:\n1. Click Sent\n2. Confirm the email is listed with the correct subject",
         "Subject: Q3 inventory adjustment summary", "Email appears in Sent with the correct subject"),
    ],
)

OUTLOOK = dict(
    path=HERE / "Outlook_Email.xlsx", case_id="TS_OUT001", role="Mailbox user",
    title="Outlook email: create, write and send a real email",
    description="Using Outlook on the web, send an email from abhosale@acsesolutions.com to "
                "abhosale@acsesolutions.com and confirm it appears in Sent Items. "
                "{now} in the Subject makes every run's email unique.",
    heading="Outlook email (create, write and send)",
    steps=[
        ("Open Outlook on the web",
         "Pre-requisite: use 'Sign in' in Automation Studio once (MFA included).\n"
         "Steps:\n1. Open Outlook on the web\n2. Wait for the mailbox to load",
         "URL: https://outlook.office.com/mail/", "The mailbox opens with the Inbox displayed"),
        ("Start a new email", "Navigation:\nInbox\nSteps:\n1. Click New mail", "",
         "A new, empty message opens"),
        ("Write the email",
         "Navigation:\nContinue from previous step\nSteps:\n1. Click To, enter the recipient and press Enter\n"
         "2. Click Subject and enter the subject\n3. Click in the message body and type the message",
         "To: abhosale@acsesolutions.com\nSubject: Automation Studio test {now}\n"
         "Body: This email was sent automatically by the Automation Studio prototype as part of test TS_OUT001.",
         "Recipient, Subject and message body are populated"),
        ("Send the email", "Navigation:\nContinue from previous step\nSteps:\n1. Click Send", "",
         "The message is sent and the new-message pane closes"),
        ("Verify the email in Sent Items",
         "Navigation:\nFolder pane\nSteps:\n1. Open Sent Items\n2. Confirm the email is listed with the subject",
         "Subject: Automation Studio test {now}", "The email appears in Sent Items with the correct subject"),
    ],
)


def build(path: Path, case_id: str, role: str, title: str, description: str, heading: str, steps) -> None:
    case = {"id": case_id, "sheet": case_id, "title": title, "description": description, "steps": [
        {"step_id": f"{case_id}.{i}", "name": name, "role": role, "description": desc,
         "data": parse_test_data(data), "expected": expected}
        for i, (name, desc, data, expected) in enumerate(steps, start=1)]}
    build_workbook(path, case, heading)
    print("written", path)


if __name__ == "__main__":
    for spec in (ACME, OUTLOOK):
        build(**spec)
