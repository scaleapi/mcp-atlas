"""User simulator tool for MCP evaluation with underspecified tasks."""

import logging
import os
from dataclasses import dataclass
from typing import List, Optional

from litellm import acompletion

logger = logging.getLogger(__name__)

# Environment variable configuration
USER_TOOL_ENABLED = os.getenv("USER_TOOL_ENABLED", "").lower() == "true"
USER_SIMULATOR_MODEL = os.getenv("USER_SIMULATOR_MODEL", "openai/gpt-4.1-2025-04-14")

# Tool definition for ask_user
ASK_USER_TOOL_DEFINITION = {
    "name": "ask_user",
    "description": "Ask the user a clarifying question to get more information about the task. Use this when the task is ambiguous or you need specific details to proceed.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The clarifying question to ask the user",
            }
        },
        "required": ["question"],
    },
}


@dataclass
class UserContext:
    """Context for simulating user responses."""

    original_prompt: str
    removed_value: List[str]
    underspecified_prompt: Optional[str] = None


async def simulate_user_response(
    question: str,
    context: str,
    user_context: UserContext,
) -> str:
    """
    Simulate a user response to a clarifying question.

    Uses an LLM to generate a response based on:
    - The original (complete) prompt
    - The removed values that created the underspecification
    - The question being asked

    Args:
        question: The clarifying question from the agent
        context: Additional context (e.g., conversation history summary)
        user_context: UserContext with original_prompt and removed_value

    Returns:
        Simulated user response string
    """
    system_prompt = """**System Role:** You are simulating a human user. You are currently in a conversation with an AI Agent who is trying to help you with a task. 

### THE SITUATION:
You have an **Original Well-Scoped Task** which represents the set of instructions you intended to provide and EVERYTHING you know about the task. However, the message you actually sent to the Agent (**Your Sent Message**) was an underspecified version where some details were removed or made vague.

The Agent is now asking you for more details, which you may or may not be able to help with.

---

### YOUR INTERNAL CONTEXT:
* **Original Well-Scoped Task (What you intended):** > {original_prompt}

* **Your Sent Message (What the Agent saw):** > {underspecified_prompt}

* **The Specific Details You Left Out:** > {removed_values}

---

### KNOWLEDGE & LOGIC BOUNDARIES:

1. **The Knowledge Limit:** You LITERALLY CAN ONLY provide any information that is explicitly stated in the **Original Well-Scoped Task**. You CANNOT answer any questions where the answer is not in the Original Well-Scoped Task.
2. **Strict Data Retrieval:**
    * If the Agent asks for a detail that is **NOT** explicitly written in the Original Well-Scoped Task, you do not know it. 
    * Do not "figure it out," guess, or use common sense to fill in gaps that aren't in your original notes.
3. **No Hand-Holding:** The Agent has its own tools and intelligence that can help it figure things out (which you can't see). Do not explain "how" to do the task or provide extra context. Just give the information that is in the Original Well-Scoped Task but not in the Sent Message.
3. **Persona:** Be a concise, direct human user. 
4. **Compound Questions:** If the agent asks for multiple details at once, and you only have information for **some** of them:
    - Provide the exact information you **do** have from the Original Well-Scoped Task.
    - For the missing parts, say something like you aren't sure or can't remember. 


REMEMBER DO NOT GUESS OR MAKE UP ANYTHING THAT IS NOT IN THE ORIGINAL WELL-SCOPED TASK.
"""

    formatted_system = system_prompt.format(
        original_prompt=user_context.original_prompt,
        underspecified_prompt=user_context.underspecified_prompt or "Not provided",
        removed_values=(
            ", ".join(user_context.removed_value)
            if user_context.removed_value
            else "None specified"
        ),
    )

    messages = [
        {"role": "system", "content": formatted_system},
        {"role": "user", "content": f"The assistant asks: {question}"},
    ]

    try:
        response = await acompletion(
            model=USER_SIMULATOR_MODEL,
            messages=messages,
            max_tokens=500,
            # temperature=0.7,
        )

        simulated_response = response.choices[0].message.content

        # logger.info("*" * 50)
        # logger.info(messages)
        # logger.info(simulated_response)
        logger.info(
            f"Simulated user response to '{question[:50]}...': {simulated_response[:100]}..."
        )
        # logger.info("*" * 50)
        return simulated_response

    except Exception as e:
        logger.error(f"Error simulating user response: {e}")
        # Return a fallback response
        return (
            "I'm not sure about that specific detail. Could you proceed with a reasonable default?"
        )
