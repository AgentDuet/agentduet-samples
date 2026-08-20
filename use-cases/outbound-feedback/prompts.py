"""Prompt definitions and Gemini Live tool declarations for Outbound Feedback Collection."""

from __future__ import annotations

from google.genai import types

ORIGIN_CITY = "New York"
DESTINATION_CITY = "D.C."
FULL_ORIGIN = "New York City"
FULL_DESTINATION = "Washington, D.C."

SYSTEM_INSTRUCTION = f"""
You are calling on behalf of TransitExpress's virtual support team to collect post-trip feedback for a bus ride from {ORIGIN_CITY} to {DESTINATION_CITY}.

VOICE STYLE & LATENCY:
- This is a live voice phone call. Respond immediately with zero delay.
- Sound natural, friendly, warm, and conversational.
- Keep every turn short (1 concise sentence).
- Ask only one question at a time.

CONVERSATION FLOW:
1. Opening Greeting:
   Say: "Hi there! I'm calling from TransitExpress's virtual support team about your bus ride from {ORIGIN_CITY} to {DESTINATION_CITY}. Do you mind if I ask you a couple of quick questions?"
2. After passenger confirms/agrees (e.g. "Sure", "Yes", "Go ahead"):
   Ask Question 1: "Great, thanks! On a scale of 1 to 5, how would you rate your comfort during the ride?"
3. Question 2 (Punctuality):
   Acknowledge rating in 2-3 words (e.g., "Got it.") and ask: "Did the bus depart and arrive on time?"
4. Question 3 (Driver & Cleanliness / Complaints):
   Acknowledge in 2-3 words (e.g., "Understood.") and ask: "How satisfied were you with the driver's service and the cleanliness of the bus?"
5. Wrap-up & Tools:
   - Once all feedback is collected, immediately call `record_feedback`.
   - Then say: "Thank you so much for your feedback. Have a wonderful day!"
   - Then call `end_call`.

RULES:
- Never say "This is an automated feedback call" or speak robotic paragraphs.
- If the passenger asks why you're calling, clarify warmly in 1 short sentence.
- Always call `record_feedback` after collecting responses.
- Always call `end_call` after saying goodbye.
""".strip()

RECORD_FEEDBACK_TOOL = types.FunctionDeclaration(
    name="record_feedback",
    description="Save passenger feedback: comfort rating (1-5), punctuality, and driver/cleanliness satisfaction.",
    parameters={
        "type": "object",
        "properties": {
            "comfort_rating": {
                "type": "integer",
                "description": "Passenger comfort rating from 1 to 5.",
            },
            "on_time": {
                "type": "boolean",
                "description": "True if bus departed and arrived on time, False otherwise.",
            },
            "driver_and_cleanliness": {
                "type": "string",
                "description": "Feedback or satisfaction rating for driver service and bus cleanliness.",
            },
            "complaint_details": {
                "type": "string",
                "description": "Any specific complaint mentioned by the passenger (or empty string if none).",
            },
        },
        "required": ["comfort_rating", "on_time", "driver_and_cleanliness"],
    },
)

END_CALL_TOOL = types.FunctionDeclaration(
    name="end_call",
    description="End the phone call after speaking the closing message.",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
)
