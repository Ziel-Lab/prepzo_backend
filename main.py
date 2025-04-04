from __future__ import annotations
import asyncio
import json
import logging
import uuid
import os
import pathlib
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict
from datetime import datetime, timezone
import ipaddress
import requests
from livekit import rtc
from livekit.agents import (
    AutoSubscribe,
    JobContext,
    WorkerOptions,
    WorkerType,
    cli,
    llm,
    multimodal
)
from livekit.plugins import google
from supabase import create_client, Client
from dotenv import load_dotenv
import prompt
from openai import OpenAI
import google.generativeai as genai
from pinecone import Pinecone  # Import Pinecone
import traceback
import dotenv

logger = logging.getLogger("my-worker")
logger.setLevel(logging.INFO)

# Initialize Supabase client
try:
    # Explicitly load environment variables first
    # No need to redefine load_dotenv
    load_dotenv()

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    if supabase_url and supabase_key:
        supabase = create_client(supabase_url, supabase_key)
        logger.info("Supabase client initialized at module level")
    else:
        logger.error("Supabase environment variables missing at module level")
        supabase = None
except Exception as e:
    logger.error(f"Failed to initialize Supabase client at module level: {e}")
    supabase = None

# Initialize OpenAI client for web search and embeddings
openai_client: OpenAI = None

# Initialize Gemini API flag
gemini_initialized: bool = False

# Initialize Pinecone client and index
pinecone_client: Pinecone = None
pinecone_index = None
PINECONE_INDEX_NAME = "coachingbooks"
EMBEDDING_MODEL = "text-embedding-3-large"  # Match the index

# Global variables for conversation tracking
conversation_history = []
session_id = None
user_message = ""
timeout_task = None
agent = None

# OpenAI TTS configuration
OPENAI_VOICE_ID = "alloy"

# Default interview preparation prompt
DEFAULT_INTERVIEW_PROMPT = prompt.prompt

# Local backup for conversations when Supabase is unreachable
LOCAL_BACKUP_DIR = pathlib.Path("./conversation_backups")
RETRY_QUEUE_FILE = LOCAL_BACKUP_DIR / "retry_queue.pkl"
MAX_RETRY_QUEUE_SIZE = 1000  # Maximum messages to store for retry

# Retry queue for failed message storage attempts
retry_queue = []

# Add a flag to control verbose logging
VERBOSE_LOGGING = False

# Add a helper for conditional logging


def verbose_log(message, level="info", error=None):
    """
    Conditionally log based on verbose setting to reduce processing
    during conversation. Only logs if VERBOSE_LOGGING is True.
    """
    if not VERBOSE_LOGGING:
        return

    if level == "info":
        logger.info(message)
    elif level == "debug":
        logger.debug(message)
    elif level == "warning":
        logger.warning(message)
    elif level == "error":
        logger.error(message)
        if error:
            logger.error(f"Error details: {error}")

# Helper function to get current UTC time


def get_utc_now():
    """Get current UTC time in a timezone-aware manner"""
    try:
        # Python 3.11+ approach
        return datetime.now(datetime.UTC)
    except AttributeError:
        # Fallback for earlier Python versions
        return datetime.now(timezone.utc)

# Helper function to get current UTC time with consistent formatting


def get_current_timestamp():
    """Get current UTC time in ISO 8601 format with timezone information"""
    return get_utc_now().isoformat()

# This function now primarily defines the tool for Gemini
# The actual search execution is handled by `handle_gemini_web_search`


def get_web_search_tool_declaration():
    """Returns the function declaration for the web search tool."""
    return {
        "name": "search_web",
        "description": (
            "Searches the web for current information on a specific topic when"
            " internal knowledge is insufficient or outdated."
            " Use only for recent"
            " events, specific factual data (like current salaries),"
            " or verifying "
            "contested information."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "search_query": {
                    "type": "string",
                    "description": (
                        "The specific, optimized query to search for "
                        "on the web."
                    )
                },
                "include_location": {
                    "type": "boolean",
                    "description": (
                        "Set to true if the user's location is relevant"
                        " to the search (e.g., local job market)."
                    )
                }
            },
            "required": ["search_query"]
        }
    }


def get_knowledge_base_tool_declaration():
    """Returns the function declaration for the knowledge base tool."""
    return {
        "name": "query_knowledge_base",
        "description": (
            "Searches an internal knowledge base of coaching books"
            " and principles for established concepts, strategies,"
            " and general career advice. Prioritize this over web search"
            " for foundational knowledge."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The specific question or topic to search for"
                        " in the knowledge base."
                    )
                }
            },
            "required": ["query"]
        }
    }

# Removed the main logic from perform_web_search as it's now split:
# 1. Declaration: get_web_search_tool_declaration()
# 2. Execution: handle_gemini_web_search() (triggered by LLM)
# 3. Underlying API call: perform_actual_search()
# --- New Tool ---
# 4. Declaration: get_knowledge_base_tool_declaration()
# 5. Execution: handle_knowledge_base_query() (triggered by LLM)
# 6. Underlying API call: query_pinecone_knowledge_base()


async def perform_actual_search(search_query):
    """
    Perform web search using Gemini API.

    Args:
        search_query (str): The search query to use

    Returns:
        str: Formatted search results text or error message
    """
    gemini_api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not gemini_api_key:
        logger.error("GEMINI_API_KEY or GOOGLE_API_KEY not set in environment variables")
        return "Unable to perform web search due to missing API key."

    try:
        logger.info(f"Querying Gemini with web search for: {search_query}")
        
        # Use Gemini model to perform web search
        genai.configure(api_key=gemini_api_key)
        model = genai.GenerativeModel('gemini-pro')
        
        # Create a prompt that explicitly asks for web search results
        prompt = f"Please search the web for: {search_query}\n\nProvide a concise summary of the most relevant information, including recent facts, key details, and sources if available. Format the response clearly with sections for different aspects of the information."
        
        response = model.generate_content(prompt)
        
        if not response or not response.text:
            logger.warning("Gemini returned empty response for web search")
            return "Web search did not return any relevant results."
            
        # Format the results
        results_text = response.text.strip()
        
        logger.info("Gemini web search successful, results generated.")
        return results_text.strip()

    except Exception as e:
        logger.error(f"Gemini web search failed: {str(e)}")
        return f"Web search failed during execution: {str(e)}"


def verify_supabase_table():
    """Verify that the conversation_histories table exists and has the correct structure"""
    if not supabase:
        logger.error("Cannot verify table: Supabase client is not initialized")
        return False

    try:
        # Check if table exists
        result = supabase.table("conversation_histories").select("*").limit(1).execute()
        logger.info("Successfully connected to conversation_histories table")

        # Log table structure
        if result.data:
            logger.info("Table structure:")
            for key in result.data[0].keys():
                logger.info(f"- {key}")
        else:
            logger.info("Table exists but is empty")

        return True
    except Exception as e:
        logger.error(f"Failed to verify conversation_histories table: {str(e)}")
        logger.error(f"Error type: {type(e)}")
        logger.error(f"Error details: {repr(e)}")
        return False

async def init_supabase():
    global supabase
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    if not supabase_url:
        logger.error("SUPABASE_URL is not set in environment variables")
        return False
    if not supabase_role_key:
        logger.error("SUPABASE_SERVICE_ROLE_KEY is not set in environment variables")
        return False

    try:
        # Log the URL (with sensitive parts redacted)
        masked_url = supabase_url.replace(supabase_role_key, '[REDACTED]')
        logger.info(f"Initializing Supabase client with URL: {masked_url}")

        supabase = create_client(supabase_url, supabase_role_key)
        logger.info("Supabase client initialized successfully")

        # Verify table structure
        if not verify_supabase_table():
            logger.error("Failed to verify conversation_histories table structure")
            return False

        # Test query to ensure we can access the table
        supabase.table("conversation_histories").select("*").limit(1).execute()
        logger.info("Successfully tested query on conversation_histories table")

        return True
    except Exception as e:
        logger.error(f"Failed to initialize Supabase client: {str(e)}")
        logger.error("Please check your SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY environment variables")
        return False

async def store_conversation_message(
    session_id: str,
    role: str,
    content: str,
    participant_id: str,
    metadata: Dict[str, Any] = None
):
    """Store a single conversation message in Supabase"""
    if not supabase:
        logger.error("Cannot store message: Supabase client is not initialized")
        return

    try:
        data = {
            "session_id": session_id,
            "role": role,
            "content": content,
            "participant_id": participant_id,
            "metadata": json.dumps(metadata) if metadata else None,
            "timestamp": datetime.utcnow().isoformat(),
        }

        logger.info(f"Attempting to store message in conversation_histories:")
        logger.info(f"Session ID: {session_id}")
        logger.info(f"Role: {role}")
        logger.info(f"Participant ID: {participant_id}")
        logger.info(f"Content length: {len(content)}")
        logger.info(f"Metadata: {metadata}")

        result = supabase.table("conversation_histories").insert(data).execute()

        if result.data:
            logger.info(f"Successfully stored message. Response data: {json.dumps(result.data, indent=2)}")
            return result
        else:
            logger.warning("Message stored but no data returned from Supabase")
            logger.warning(f"Full result object: {result}")
            return None

    except Exception as e:
        logger.error(f"Failed to store message in Supabase: {str(e)}")
        logger.error(f"Error type: {type(e)}")
        logger.error(f"Error details: {repr(e)}")
        logger.error(f"Data being stored: {json.dumps(data, indent=2)}")
        return None

async def store_full_conversation():
    """Store the entire conversation history to Supabase."""
    global conversation_history, session_id

    if not supabase:
        logger.error("Cannot store full conversation: Supabase client is not initialized")
        return

    if not session_id:
        logger.error("Cannot store full conversation: session_id is not set")
        return

    if not conversation_history:
        logger.info("No conversation history to store")
        return

    try:
        # Filter out system messages from the conversation history
        filtered_conversation = [msg for msg in conversation_history if msg.get("role") != "system"]

        # Get participant_id from the conversation history or use default
        participant_id = "unknown"
        for msg in conversation_history:
            if msg.get("role") == "user" and "metadata" in msg and "participant_identity" in msg.get("metadata", {}):
                participant_id = msg["metadata"]["participant_identity"]
                break

        # Create the conversation object with only the essential fields
        conversation_data = {
            "session_id": session_id,
            "participant_id": participant_id,
            "conversation": filtered_conversation,  # Store only non-system messages
            "timestamp": datetime.utcnow().isoformat()
        }

        logger.info(f"Storing conversation with {len(filtered_conversation)} messages")
        logger.info(f"Last message in history: {json.dumps(filtered_conversation[-1] if filtered_conversation else None, indent=2)}")

        # Check if a record already exists for this session
        check_query = supabase.table("conversation_histories").select("*").eq("session_id", session_id).execute()

        if check_query.data:
            logger.info(f"Updating existing conversation record for session {session_id}")
            result = supabase.table("conversation_histories").update(conversation_data).eq("session_id", session_id).execute()
        else:
            logger.info(f"Creating new conversation record for session {session_id}")
            result = supabase.table("conversation_histories").insert(conversation_data).execute()

        if result.data:
            logger.info(f"Successfully stored conversation. Response data: {json.dumps(result.data, indent=2)}")
            return result
        else:
            logger.warning("Store operation completed but returned no data")
            logger.warning(f"Full result object: {result}")
            return None

    except Exception as e:
        logger.error(f"Failed to store conversation: {str(e)}")
        logger.error(f"Error type: {type(e)}")
        logger.error(f"Error details: {repr(e)}")
        # Log the conversation history for debugging
        try:
            logger.error(f"Conversation data being stored: {json.dumps(conversation_data, indent=2)}")
        except Exception as debug_err:
            logger.error(f"Error logging conversation data: {str(debug_err)}")
        return None

# Function to check Supabase connection health


async def check_supabase_health():
    """
    Check if Supabase connection is healthy and reconnect if needed.
    Returns True if connection is healthy, False otherwise.
    """
    global supabase

    if not supabase:
        logger.warning(
            "Supabase client not initialized, attempting to initialize")
        return await init_supabase()

    try:
        # Test the connection with a simple query
        test_result = supabase.table("conversation_histories").select(
            "session_id").limit(1).execute()
        logger.debug("Supabase connection is healthy")
        return True
    except Exception as e:
        logger.warning(
            f"Supabase connection error: {e}, attempting to reconnect")
        return await init_supabase()

# Optimize store_conversation_message to be less resource-intensive


async def store_conversation_message(session_id, participant_id, conversation):
    """Store a conversation message in Supabase, with local backup on failure"""

    # Debug: Print environment variables
    logger.error(f"SUPABASE_URL: {os.environ.get('SUPABASE_URL')}")
    logger.error(f"SUPABASE_SERVICE_ROLE_KEY exists: {bool(os.environ.get('SUPABASE_SERVICE_ROLE_KEY'))}")
    logger.error(f"Global supabase client exists: {supabase is not None}")

    # Skip non-essential database operations if agent is actively speaking
    is_speaking = agent and hasattr(agent, 'is_speaking') and agent.is_speaking

    # For system messages during speech, defer to retry queue instead of
    # immediate storage
    if is_speaking and participant_id == "system":
        add_to_retry_queue(session_id, participant_id, conversation)
        return False

    # Continue with existing storage logic
    # Check Supabase connection first
    supabase_available = await check_supabase_health()
    logger.error(f"Supabase health check result: {supabase_available}")

    if not supabase_available:
        logger.error(
            "Supabase client not available, adding message to retry queue")
        add_to_retry_queue(session_id, participant_id, conversation)
        return False

    # Continue with existing logic
    if not conversation:
        logger.error("Message data is empty, skipping storage")
        return False

    # Create a message ID if not present
    message_id = conversation.get("message_id", str(uuid.uuid4()))

    # Update message data to ensure it has a message_id
    if "message_id" not in conversation:
        conversation["message_id"] = message_id

    # Ensure we're using a serializable format for conversation
    try:
        # Convert the conversation to a proper JSON object for the jsonb column
        serialized_conversation = json.dumps(conversation) if isinstance(
            conversation, dict) else conversation

        # Extract transcript for raw_conversation field
        content = conversation.get("content", "")

        # Get participant email if available, or use default
        user_email = ""
        if isinstance(conversation, dict) and "metadata" in conversation:
            metadata = conversation.get("metadata", {})
            if isinstance(metadata, dict) and "user_email" in metadata:
                user_email = metadata.get("user_email", "")

        # Get the timestamp
        timestamp = conversation.get("timestamp", get_current_timestamp())

        # Calculate message count (here just set to 1 for individual messages)
        message_count = 1

    except TypeError as e:
        logger.error(f"Failed to serialize message data: {e}")
        # Create a simplified version without problematic fields
        safe_message = {
            "role": conversation.get("role", "unknown"),
            "content": conversation.get("content", ""),
            "timestamp": conversation.get("timestamp", get_current_timestamp()),
            "message_id": message_id
        }
        serialized_conversation = json.dumps(safe_message)
        content = safe_message.get("content", "")
        timestamp = safe_message.get("timestamp", get_current_timestamp())
        message_count = 1
        user_email = ""

    # Prepare the exact insert_data structure matching Supabase table columns
    insert_data = {
        "session_id": session_id,
        "participant_id": participant_id,
        "conversation": serialized_conversation,
        "raw_conversation": content,  # Use content as raw_conversation
        "message_count": message_count,
        "user_email": user_email,
        "timestamp": timestamp,
        "email_sent": False  # Default to false for new messages
    }

    # Debug: Print the data being sent to Supabase
    logger.error(f"Attempting to store message for session: {session_id}")
    logger.error(f"Insert data: {json.dumps({k: v for k, v in insert_data.items() if k != 'conversation'})}")

    try:
        # Use upsert with the documented format from Supabase docs - NOT async
        logger.error(f"Supabase client before upsert: {supabase is not None}")
        response = (
            supabase.table("conversation_histories")
            # Use appropriate conflict resolution
            .upsert([insert_data], on_conflict="session_id,timestamp")
            .execute()
        )

        # Debug: Print response details
        logger.error(f"Supabase response: {response}")
        logger.error(f"Response has data attribute: {hasattr(response, 'data')}")
        if hasattr(response, 'data'):
            logger.error(f"Response data: {response.data}")
        logger.error(f"Response has error attribute: {hasattr(response, 'error')}")
        if hasattr(response, 'error'):
            logger.error(f"Response error: {response.error}")

        # Check response structure following Supabase pattern
        if hasattr(response, 'data') and response.data:
            logger.info(f"Successfully stored message with ID: {message_id}")
            return True
        elif hasattr(response, 'error') and response.error:
            logger.error(f"Supabase upsert error: {response.error}")
            add_to_retry_queue(session_id, participant_id, conversation)
            return False
        else:
            # Handle unexpected response structure
            logger.warning(f"Unexpected response from Supabase: {response}")
            add_to_retry_queue(session_id, participant_id, conversation)
            return False

    except Exception as e:
        error_type = type(e).__name__
        error_message = str(e)

        # Debug: Print full exception details
        logger.error(f"Exception during Supabase storage: {error_type}: {error_message}")
        logger.error(f"Exception traceback: {traceback.format_exc()}")

        # Log different error types differently
        if "duplicate key" in error_message.lower():
            verbose_log(f"Message already exists in database (duplicate key): {error_message}", level="warning")
            return True 
        elif "permission" in error_message.lower():
            logger.error(f"Supabase permission error: {error_message}")
        elif "network" in error_message.lower() or "timeout" in error_message.lower():
            logger.error(f"Supabase network error: {error_message}")
        else:
            logger.error(f"Failed to store message: {error_type}: {error_message}")
            # Only log full trace in verbose mode
            verbose_log("Error details: " + traceback.format_exc(), level="error")

        # Add to retry queue for later processing
        add_to_retry_queue(session_id, participant_id, conversation)
        return False


async def store_full_conversation():
    """
    Store the entire conversation history to Supabase.
    This function is called at key points to ensure the conversation is saved.
    """
    global conversation_history, session_id

    if not supabase:
        logger.error(
            "Cannot store full conversation: Supabase client is not initialized")
        return

    if not session_id:
        logger.error("Cannot store full conversation: session_id is not set")
        return

    if not conversation_history:
        logger.info("No conversation history to store")
        return

    try:
        # Count how many messages need to be stored
        to_store_count = sum(
    1 for msg in conversation_history if not msg.get(
        "metadata", {}).get(
            "stored", False))
        

        if to_store_count == 0:
            logger.info("All messages already stored")
            return

        # Collect messages for batch upsert if possible
        messages_to_store = []

        for message in conversation_history:
            if message.get("metadata", {}).get("stored", False):
                continue  # Skip messages that have already been stored

            # If message doesn't have a message_id yet, add one
            if "message_id" not in message:
                message["message_id"] = str(uuid.uuid4())

            # Prepare the message for storage with serialization error handling
            try:
                # Ensure we're using a serializable format for conversation
                serialized_message = json.dumps(
                    message) if isinstance(message, dict) else message

                # Extract transcript for raw_conversation field
                content = message.get("content", "")

                # Get participant email if available, or use default
                user_email = ""
                if isinstance(message, dict) and "metadata" in message:
                    metadata = message.get("metadata", {})
                    if isinstance(metadata, dict) and "user_email" in metadata:
                        user_email = metadata.get("user_email", "")

                # Get the timestamp
                timestamp = message.get("timestamp", get_current_timestamp())

                # Get participant_id or use role as fallback
                participant_id = message.get(
    "participant_id", message.get(
        "role", "unknown"))

                # Create an insert item matching the Supabase table structure
                insert_data = {
                    "session_id": session_id,
                    "participant_id": participant_id,
                    "conversation": serialized_message,
                    "raw_conversation": content,
                    "message_count": 1,
                    "user_email": user_email,
                    "timestamp": timestamp,
                    "email_sent": False
                }

                messages_to_store.append(insert_data)
            except TypeError as e:
                logger.error(
    f"Failed to serialize message for batch storage: {e}")
                # Create a safe version without problematic fields
                try:
                    safe_message = {
                        "role": message.get("role", "unknown"),
                        "content": message.get("content", ""),
                        "timestamp": message.get("timestamp", get_current_timestamp()),
                        "message_id": message.get("message_id")
                    }

                    serialized_safe_message = json.dumps(safe_message)
                    content = safe_message.get("content", "")
                    timestamp = safe_message.get(
    "timestamp", get_current_timestamp())
                    participant_id = message.get(
    "participant_id", message.get(
        "role", "unknown"))

                    insert_data = {
                        "session_id": session_id,
                        "participant_id": participant_id,
                        "conversation": serialized_safe_message,
                        "raw_conversation": content,
                        "message_count": 1,
                        "user_email": "",
                        "timestamp": timestamp,
                        "email_sent": False
                    }

                    messages_to_store.append(insert_data)
                except Exception as safe_err:
                    logger.error(
    f"Could not create safe version of message: {safe_err}")

        # Store messages in batches to avoid overwhelming Supabase
        batch_size = 10
        success_count = 0

        for i in range(0, len(messages_to_store), batch_size):
            batch = messages_to_store[i:i + batch_size]
            try:
                # Use proper upsert with array pattern - Supabase Python client
                # is not async
                response = (
                    supabase.table("conversation_histories")
                    .upsert(batch, on_conflict="session_id,timestamp")
                    .execute()
                )

                if hasattr(response, 'data') and response.data:
                    # Mark messages as stored
                    for j, msg_data in enumerate(batch):
                        # Find the corresponding message in
                        # conversation_history
                        for msg in conversation_history:
                            # Match by timestamp since message_id might not be
                            # in the database structure
                            if (msg.get("timestamp") == msg_data["timestamp"] and
                                msg.get("role", "unknown") == msg_data["participant_id"]):
                                if "metadata" not in msg:
                                    msg["metadata"] = {}
                                msg["metadata"]["stored"] = True
                                success_count += 1
                                break
                else:
                    logger.warning(
    f"Unexpected response from batch upsert: {response}")
            except Exception as e:
                logger.error(f"Error during batch upsert: {e}")
                # Individual fallback for failed batch
                for msg_data in batch:
                    try:
                        # Individual storage with more detailed error handling
                        single_response = (
                            supabase.table("conversation_histories")
                            .upsert([msg_data], on_conflict="session_id,timestamp")
                            .execute()
                        )

                        if hasattr(
    single_response,
     'data') and single_response.data:
                            for msg in conversation_history:
                                if (msg.get("timestamp") == msg_data["timestamp"] and
                                    msg.get("role", "unknown") == msg_data["participant_id"]):
                                    if "metadata" not in msg:
                                        msg["metadata"] = {}
                                    msg["metadata"]["stored"] = True
                                    success_count += 1
                                    break
                    except Exception as single_error:
                        logger.error(
    f"Error in fallback single storage: {single_error}")

            # Small delay between batches
            await asyncio.sleep(0.5)

        # Verify success rate
        logger.info(
    f"Successfully stored {success_count}/{to_store_count} messages")

        # Final verification - important for debugging
        total_stored = sum(
    1 for msg in conversation_history if msg.get(
        "metadata", {}).get(
            "stored", False))
        logger.info(
            f"Total messages marked as stored: {total_stored}/{len(conversation_history)}")

    except Exception as e:
        logger.error(f"Failed to store full conversation: {str(e)}")
        logger.error(f"Error type: {type(e)}")
        logger.error(f"Full error details: {repr(e)}")
        # Emergency backup disabled to prevent serialization errors

# Add a helper method to directly await the storage task when critical


async def ensure_storage_completed():
    """Ensure that conversation storage is completed before proceeding."""
    try:
        await store_full_conversation()
        logger.info("Storage operation completed")
        return True
    except Exception as e:
        logger.error(f"Error during ensured storage: {str(e)}")
        return False


def load_env_files():
    # Get the paths to both .env files
    agent_env_path = pathlib.Path(__file__).parent / '.env'
    web_env_path = pathlib.Path(__file__).parent.parent / 'web' / '.env.local'

    logger.info("Agent .env path: %s", agent_env_path.absolute())
    logger.info("Web .env.local path: %s", web_env_path.absolute())
    logger.info("Agent .env exists: %s", agent_env_path.exists())
    logger.info("Web .env.local exists: %s", web_env_path.exists())

    # Try to load from web .env.local first
    if web_env_path.exists():
        load_dotenv(dotenv_path=web_env_path, override=True)
        logger.info("Loaded environment from web .env.local")

    # Then load from agent .env (will override if exists)
    if agent_env_path.exists():
        load_dotenv(dotenv_path=agent_env_path, override=True)
        logger.info("Loaded environment from agent .env")

    # Debug log all environment variables
    logger.info("Environment variables after loading:")
    logger.info("LIVEKIT_URL: %s", os.environ.get('LIVEKIT_URL', 'NOT SET'))
    logger.info(
    "LIVEKIT_API_KEY: %s",
    os.environ.get(
        'LIVEKIT_API_KEY',
         'NOT SET'))
    logger.info("OPENAI_API_KEY exists: %s",
     os.environ.get('OPENAI_API_KEY') is not None)
    logger.info("ELEVENLABS_API_KEY exists: %s",
     os.environ.get('ELEVENLABS_API_KEY') is not None)
    logger.info("GEMINI_API_KEY exists: %s",
     os.environ.get('GEMINI_API_KEY') is not None)
    logger.info("GOOGLE_API_KEY exists: %s",
     os.environ.get('GOOGLE_API_KEY') is not None)
    logger.info("GOOGLE_APPLICATION_CREDENTIALS exists: %s",
     os.environ.get('GOOGLE_APPLICATION_CREDENTIALS') is not None)

    if os.environ.get('OPENAI_API_KEY'):
        api_key = os.environ.get('OPENAI_API_KEY', '')
        logger.info("OPENAI_API_KEY starts with: %s",
                    api_key[:15] if api_key else 'EMPTY')

    if os.environ.get('ELEVENLABS_API_KEY'):
        api_key = os.environ.get('ELEVENLABS_API_KEY', '')
        logger.info("ELEVENLABS_API_KEY starts with: %s",
                    api_key[:15] if api_key else 'EMPTY')

    if os.environ.get('GEMINI_API_KEY'):
        api_key = os.environ.get('GEMINI_API_KEY', '')
        logger.info("GEMINI_API_KEY starts with: %s",
                    api_key[:15] if api_key else 'EMPTY')

        # Initialize Gemini API if key is available
        try:
            genai.configure(api_key=api_key)
            global gemini_initialized
            gemini_initialized = True
            logger.info("Gemini API initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Gemini API: {str(e)}")

    # Try reading both .env files directly
    try:
        if agent_env_path.exists():
            with open(agent_env_path, 'r') as f:
                lines = f.readlines()
                if lines:
                    logger.info("Agent .env first line: %s", lines[0].strip())
    except Exception as e:
        logger.error("Error reading agent .env file: %s", str(e))

    try:
        if web_env_path.exists():
            with open(web_env_path, 'r') as f:
                lines = f.readlines()
                if lines:
                    logger.info(
    "Web .env.local first line: %s",
     lines[0].strip())
    except Exception as e:
        logger.error("Error reading web .env.local file: %s", str(e))

# Function to get user's IP location data


def get_ip_location(ip_address: str) -> Dict[str, Any]:
    """
    Get location information from an IP address using a free IP geolocation API.
    Tries multiple APIs in case one fails.

    Args:
        ip_address: The IP address to look up

    Returns:
        A dictionary with location information or empty dict if not found
    """
    if not ip_address:
        logger.warning("No IP address provided for geolocation")
        return {}

    try:
        # Skip private IP addresses
        try:
            if ipaddress.ip_address(ip_address).is_private:
                logger.info(
    f"Skipping geolocation for private IP: {ip_address}")
                return {}
        except ValueError:
            logger.warning(f"Invalid IP address format: {ip_address}")
            return {}

        # Try ip-api.com first (free, no API key required)
        try:
            logger.info(
    f"Attempting geolocation lookup for IP: {ip_address} via ip-api.com")
            url = f"http://ip-api.com/json/{ip_address}"
            response = requests.get(url, timeout=5)

            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "success":
                    logger.info(
    f"Successfully retrieved geolocation data for {ip_address}")
                    location_data = {
                        "country": data.get("country"),
                        "region": data.get("regionName"),
                        "city": data.get("city"),
                        "timezone": data.get("timezone"),
                        "lat": data.get("lat"),
                        "lon": data.get("lon"),
                        "isp": data.get("isp"),
                        "source": "ip-api.com"
                    }
                    logger.info(f"Location data: {json.dumps(location_data)}")
                    return location_data
                else:
                    logger.warning(
                        f"ip-api.com returned non-success status for {ip_address}: {data.get('status', 'unknown')}")
        except Exception as e:
            logger.warning(
                f"Error with ip-api.com: {str(e)}, trying fallback API")

        # Fallback to ipinfo.io (has free tier with rate limits)
        try:
            logger.info(
    f"Attempting fallback geolocation lookup for IP: {ip_address} via ipinfo.io")
            ipinfo_token = os.environ.get("IPINFO_TOKEN", "")
            url = f"https://ipinfo.io/{ip_address}/json"
            if ipinfo_token:
                url += f"?token={ipinfo_token}"

            response = requests.get(url, timeout=5)

            if response.status_code == 200:
                data = response.json()
                if "bogon" not in data and "error" not in data:
                    # Parse location from format like "City, Region, Country"
                    loc_parts = (data.get("loc", "").split(
                        ",") if data.get("loc") else [])
                    lat = loc_parts[0] if len(loc_parts) > 0 else None
                    lon = loc_parts[1] if len(loc_parts) > 1 else None

                    location_data = {
                        "country": data.get("country"),
                        "region": data.get("region"),
                        "city": data.get("city"),
                        "timezone": data.get("timezone"),
                        "lat": lat,
                        "lon": lon,
                        "isp": data.get("org"),
                        "source": "ipinfo.io"
                    }
                    logger.info(
    f"Successfully retrieved fallback location data for {ip_address}")
                    logger.info(f"Location data: {json.dumps(location_data)}")
                    return location_data
                else:
                    logger.warning(
    f"ipinfo.io indicates invalid IP: {ip_address}")
        except Exception as e:
            logger.warning(f"Error with ipinfo.io: {str(e)}")

        # If all methods fail, try to get a default or estimated location
        # Use environment variable if available
        default_country = os.environ.get("DEFAULT_COUNTRY", "")
        default_city = os.environ.get("DEFAULT_CITY", "")
        default_timezone = os.environ.get("DEFAULT_TIMEZONE", "")

        if default_country or default_city or default_timezone:
            logger.info(f"Using default location from environment variables")
            return {
                "country": default_country,
                "city": default_city,
                "timezone": default_timezone,
                "source": "default_environment"
            }

        logger.warning(
    f"Failed to get location for IP {ip_address} using all methods")
        return {}
    except Exception as e:
        logger.error(f"Error getting IP location for {ip_address}: {str(e)}")
        logger.error(f"Error type: {type(e)}")
        logger.error(f"Full error details: {repr(e)}")
        return {}

# Function to get local time based on timezone


def get_local_time(timezone: str) -> Dict[str, Any]:
    """
    Get local time details for a given timezone

    Args:
        timezone: The timezone string (e.g., 'America/New_York', 'UTC+2', etc.)

    Returns:
        Dictionary with local time information
    """
    if not timezone:
        logger.warning("No timezone provided for local time determination")
        return {
            "local_time": get_utc_now().strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "UTC",
            "time_of_day": "unknown",
            "is_business_hours": False,
            "day_of_week": get_utc_now().strftime("%A"),
            "source": "fallback_utc"
        }

    try:
        # Try to determine offset from various timezone formats
        utc_now = get_utc_now()
        offset_hours = 0

        # Check different timezone format patterns
        if timezone.startswith("UTC+") or timezone.startswith("GMT+"):
            try:
                offset_str = timezone.split("+")[1]
                # Handle hours:minutes format
                if ":" in offset_str:
                    hours, minutes = offset_str.split(":")
                    offset_hours = int(hours) + (int(minutes) / 60)
                else:
                    offset_hours = float(offset_str)
            except (IndexError, ValueError) as e:
                logger.warning(
                    f"Error parsing positive UTC offset from {timezone}: {str(e)}"
                )


        elif timezone.startswith("UTC-") or timezone.startswith("GMT-"):
            try:
                offset_str = timezone.split("-")[1]
                # Handle hours:minutes format
                if ":" in offset_str:
                    hours, minutes = offset_str.split(":")
                    offset_hours = -1 * (int(hours) + (int(minutes) / 60))
                else:
                    offset_hours = -1 * float(offset_str)
            except (IndexError, ValueError) as e:
                logger.warning(
                    f"Error parsing negative UTC offset from {timezone}: {str(e)}"
                )

        # Named timezone handling would be better with pytz, but we can do a simple map
        # for common timezones if pytz is not available
        timezone_map = {
            "America/New_York": -5,  # EST
            "America/Los_Angeles": -8,  # PST
            "America/Chicago": -6,  # CST
            "America/Denver": -7,  # MST
            "Europe/London": 0,  # GMT
            "Europe/Paris": 1,  # CET
            "Europe/Berlin": 1,  # CET
            "Europe/Moscow": 3,
            "Asia/Tokyo": 9,
            "Asia/Shanghai": 8,
            "Asia/Dubai": 4,
            "Asia/Singapore": 8,
            "Australia/Sydney": 10,
            "Pacific/Auckland": 12
        }

        # Check if we have this timezone in our map
        if timezone in timezone_map:
            offset_hours = timezone_map[timezone]
            logger.info(
    f"Found timezone {timezone} in map with offset {offset_hours}")

        # Calculate hours accounting for daylight saving time (very simple approach)
        # In a real implementation, use a proper timezone library like pytz or
        # dateutil
        month = utc_now.month
        is_northern_summer = 3 <= month <= 10  # Very rough DST approximation

        # Adjust for daylight saving time
        if is_northern_summer and timezone in [
            "America/New_York", "America/Chicago", "America/Denver",
            "America/Los_Angeles", "Europe/London", "Europe/Paris", "Europe/Berlin"
        ]:
            offset_hours += 1
            logger.info(
    f"Adjusted for daylight saving time, new offset: {offset_hours}")

        # Calculate local time based on offset
        # Add the offset hours
        hour_delta = int(offset_hours)
        minute_delta = int((offset_hours - hour_delta) * 60)

        # Add hours and minutes to create the local time
        local_time = utc_now
        local_time = local_time.replace(hour=(utc_now.hour + hour_delta) % 24)
        if minute_delta != 0:
            local_time = local_time.replace(
    minute=(
        utc_now.minute +
        minute_delta) %
         60)
            if utc_now.minute + minute_delta >= 60:
                local_time = local_time.replace(
                    hour=(local_time.hour + 1) % 24)

        # Handle day rollover for negative or positive offsets
        day_offset = 0
        if utc_now.hour + hour_delta < 0:
            day_offset = -1
        elif utc_now.hour + hour_delta >= 24:
            day_offset = 1

        # Apply day offset if needed
        if day_offset != 0:
            # This is a simplified approach without proper day/month boundary handling
            # In production, use a proper datetime library
            local_date = utc_now.date()
            local_time = local_time.replace(day=(local_date.day + day_offset))

        # Simple time categories for contextual understanding
        hour = local_time.hour
        if 5 <= hour < 12:
            time_of_day = "morning"
        elif 12 <= hour < 17:
            time_of_day = "afternoon"
        elif 17 <= hour < 22:
            time_of_day = "evening"
        else:
            time_of_day = "night"

        # Create the full local time context
        result = {
            "local_time": local_time.strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": timezone,
            "timezone_offset": f"UTC{'+' if offset_hours >= 0 else ''}{offset_hours}",
            "time_of_day": time_of_day,
            "is_business_hours": 9 <= hour < 17 and local_time.weekday() < 5,
            "day_of_week": local_time.strftime("%A"),
            "date": local_time.strftime("%Y-%m-%d"),
            "approximate_dst": is_northern_summer,
            "source": "calculated"
        }

        logger.info(
            f"Calculated local time for timezone {timezone}: {result['local_time']}"
        )
        return result

    except Exception as e:
        logger.error(
            f"Error calculating local time for timezone {timezone}: {str(e)}"
        )
        logger.error(f"Error type: {type(e)}")
        logger.error(f"Full error details: {repr(e)}")

        # Return UTC time as fallback
        utc_now = get_utc_now()
        return {
            "local_time": utc_now.strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "UTC",
            "time_of_day": "unknown",
            "is_business_hours": False,
            "day_of_week": utc_now.strftime("%A"),
            "date": utc_now.strftime("%Y-%m-%d"),
            "source": "fallback_error"
        }

# Extract client IP address from participant


def extract_client_ip(participant: rtc.Participant) -> str:
    """
    Try to extract the client IP address from participant information.
    This is a best-effort approach that attempts multiple methods to find the IP.

    Args:
        participant: The LiveKit participant

    Returns:
        The IP address as a string, or empty string if not found
    """
    try:
        # First, check if the IP is in metadata as a direct field
        if participant.metadata:
            try:
                metadata = json.loads(participant.metadata)
                # Direct ip_address field
                if metadata.get("ip_address"):
                    logger.info(
                        f"Found IP address in metadata.ip_address: {metadata.get('ip_address')}"
                    )
                    return metadata.get("ip_address")

                # Check headers if provided
                if metadata.get("headers"):
                    headers = metadata.get("headers", {})
                    # Common headers that might contain the real IP
                    ip_headers = [
                        "X-Forwarded-For",
                        "X-Real-IP",
                        "CF-Connecting-IP",  # Cloudflare
                        "True-Client-IP",    # Akamai/Cloudflare
                        "X-Client-IP"        # Amazon CloudFront
                    ]

                    for header in ip_headers:
                        if headers.get(header):
                            # X-Forwarded-For can contain multiple IPs - take
                            # the first one
                            ip = headers.get(header).split(',')[0].strip()
                            logger.info(
    f"Found IP address in header {header}: {ip}")
                            return ip
            except json.JSONDecodeError as e:
                logger.error(
                    f"Error parsing participant metadata for IP: {str(e)}"
                )

        # Try to get IP from connection info if LiveKit makes it available
        # This is implementation-specific and depends on what LiveKit makes
        # available
        if hasattr(
    participant,
    "connection_info") and hasattr(
        participant.connection_info,
         "client_ip"):
            logger.info(
                f"Found IP in connection_info: {participant.connection_info.client_ip}"
            )
            return participant.connection_info.client_ip

        # If we couldn't find an IP, log this for debugging
        logger.warning("Could not determine client IP address")
        logger.info(f"Participant identity: {participant.identity}")
        logger.info(f"Participant metadata type: {type(participant.metadata)}")
        if participant.metadata:
            logger.info(f"Metadata content: {participant.metadata[:100]}...")

        # Use a placeholder default if needed for development
        default_ip = os.environ.get("DEFAULT_TEST_IP", "")
        if default_ip:
            logger.info(f"Using default test IP address: {default_ip}")
            return default_ip

        return ""
    except Exception as e:
        logger.error(f"Error extracting client IP: {str(e)}")
        logger.error(f"Error type: {type(e)}")
        logger.error(f"Full error details: {repr(e)}")
        return ""


# Load environment variables at startup
load_env_files()


@dataclass
class SessionConfig:
    instructions: str
    voice: str  # ElevenLabs voice ID
    temperature: float
    max_response_output_tokens: str | int
    modalities: list[str]
    turn_detection: Dict[str, Any]

    def __post_init__(self):
        if self.modalities is None:
            self.modalities = self._modalities_from_string("text_and_audio")
        # Set default voice to ElevenLabs voice ID if not specified
        if not self.voice:
            self.voice = OPENAI_VOICE_ID

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def _modalities_from_string(modalities: str) -> list[str]:
        modalities_map = {
            "text_and_audio": ["text", "audio"],
            "text_only": ["text"],
        }
        return modalities_map.get(modalities, ["text", "audio"])

    def __eq__(self, other: SessionConfig) -> bool:
        return self.to_dict() == other.to_dict()


def parse_session_config(data: Dict[str, Any]) -> SessionConfig:
    turn_detection = None

    if data.get("turn_detection"):
        turn_detection = json.loads(data.get("turn_detection"))
    else:
        turn_detection = {
            "threshold": 0.5,
            "prefix_padding_ms": 200,
            "silence_duration_ms": 300,
        }

    # Use default prompt if none provided
    config = SessionConfig(
        instructions=data.get("instructions", DEFAULT_INTERVIEW_PROMPT),
        voice=data.get("voice", OPENAI_VOICE_ID),
        temperature=float(data.get("temperature", 0.8)),
        max_response_output_tokens=data.get("max_output_tokens")
        if data.get("max_output_tokens") == "inf"
        else int(data.get("max_output_tokens") or 2048),
        modalities=SessionConfig._modalities_from_string(
            data.get("modalities", "text_and_audio")
        ),
        turn_detection=turn_detection,
    )
    return config


async def entrypoint(ctx: JobContext):
    load_env_files()

    try:
        if not await init_supabase():
            logger.error("Failed to initialize Supabase in worker process")
            raise Exception("Database connection failed")

        # Initialize OpenAI client for web search
        global openai_client
        openai_api_key = os.environ.get("OPENAI_API_KEY")
        if not openai_api_key:
            logger.error(
                "OPENAI_API_KEY not set, web search functionality will be disabled")
        else:
            try:
                openai_client = OpenAI(api_key=openai_api_key)
                logger.info("OpenAI client initialized for web search")
            except Exception as openai_error:
                logger.error(
                    f"Failed to initialize OpenAI client: {str(openai_error)}"
                )

        logger.info(f"connecting to room {ctx.room.name}")
        try:
            await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
            logger.info("Successfully connected to room")
        except Exception as conn_error:
            logger.error(f"Failed to connect to room: {str(conn_error)}")
            raise

        logger.info("Waiting for participant...")
        try:
            participant = await ctx.wait_for_participant()
            logger.info(f"Participant joined: {participant.identity}")
            logger.info(f"Participant metadata: {participant.metadata}")
        except Exception as part_error:
            logger.error(f"Error waiting for participant: {str(part_error)}")
            raise

        try:
            await run_multimodal_agent(ctx, participant)
        except Exception as agent_error:
            logger.error(f"Error in multimodal agent: {str(agent_error)}")
            logger.error(
                f"Participant details - Identity: { participant.identity}, Name: { participant.name}"
            )
            logger.error(
                f"Participant metadata type: { type(participant.metadata)}"
            )
            logger.error(f"Raw metadata content: {repr(participant.metadata)}")
            raise

        logger.info("agent started successfully")

    except Exception as e:
        logger.error(f"Critical error in entrypoint: {str(e)}")
        logger.error(f"Error type: {type(e)}")
        logger.error(f"Full error details: {repr(e)}")
        raise


async def run_multimodal_agent(ctx: JobContext, participant: rtc.Participant):
    global conversation_history, session_id, timeout_task
    # Ensure all tasks are properly declared
    global retry_processor_task, periodic_saver_task

    # Initialize task variables to None
    timeout_task = None
    periodic_saver_task = None
    retry_processor_task = None
    connection_checker_task = None

    try:
        # Capture initial metadata to associate with transcript
        logger.info(f"Participant metadata raw: '{participant.metadata}'")
        logger.info(f"Participant identity: {participant.identity}")

        # Store LiveKit session details for better transcript association
        room_name = ctx.room.name
        room_sid = getattr(ctx.room, 'sid', 'unknown')
        participant_identity = participant.identity
        participant_sid = getattr(participant, 'sid', 'unknown')

        # Use LiveKit room name directly as session_id (no UUID generation)
        session_id = room_name
        logger.info(f"Using LiveKit room name as session ID: {session_id}")
        logger.info(
    f"LiveKit Room SID: {room_sid}, Participant SID: {participant_sid}")

        # Store initial session metadata for transcript context
        initial_session_metadata = {
            "role": "system",
            "content": "Session started",
            "timestamp": get_current_timestamp(),
            "metadata": {
                "type": "session_start",
                "room_name": room_name,
                "room_sid": room_sid,
                "participant_identity": participant_identity,
                "participant_sid": participant_sid,
                "session_id": session_id,
                "start_time": get_current_timestamp()
            }
        }

        # Add to conversation history
        conversation_history = [initial_session_metadata]

        # Store initial metadata immediately
        await store_conversation_message(
            session_id=session_id,
            participant_id="system",
            conversation=initial_session_metadata
        )

        # Auto-disconnect detection and handling
        async def check_connection_status():
            """Check if the connection is still alive by monitoring activity"""
            inactivity_threshold = 300  # 5 minutes of inactivity triggers a disconnect
            last_activity_time = time.time()  # Initialize with current time

            try:
                while True:
                    # Increased from 60 to 120 seconds to reduce check
                    # frequency
                    await asyncio.sleep(120)  # Check every 2 minutes

                    # Update last activity time if we have recent messages
                    if conversation_history:
                        latest_msg_time = conversation_history[-1].get(
                            "timestamp", 0)
                        if isinstance(latest_msg_time, str):
                            try:
                                # Try to parse the timestamp if it's a string
                                latest_dt = datetime.fromisoformat(
                                    latest_msg_time.replace('Z', '+00:00'))
                                latest_msg_time = latest_dt.timestamp()
                            except ValueError:
                                latest_msg_time = 0

                        if latest_msg_time > last_activity_time:
                            last_activity_time = latest_msg_time

                    # Check inactivity duration
                    inactivity_duration = time.time() - last_activity_time
                    if inactivity_duration > inactivity_threshold:
                        logger.warning(
                            f"Auto-disconnect triggered after {inactivity_duration:.1f} seconds of inactivity")

                        # Add a system message about auto-disconnect
                        system_msg = {
                            "role": "system",
                            "content": "Call automatically ended due to inactivity.",
                            "timestamp": get_current_timestamp()
                        }

                        if system_msg not in conversation_history:
                            conversation_history.append(system_msg)
                            # Store in background task without awaiting
                            asyncio.create_task(store_conversation_message(
                                session_id=session_id,
                                participant_id="system",
                                conversation=system_msg
                            ))

                        # Force disconnection
                        try:
                            # Force disconnect after storing the final state
                            raise ForceDisconnectError(
                                "Auto-disconnect triggered due to inactivity")
                        except Exception as e:
                            logger.error(f"Error during auto-disconnect: {e}")
                            break

            except asyncio.CancelledError:
                logger.info("Connection status checker task cancelled")
            except Exception as e:
                logger.error(f"Error in connection status checker: {e}")

        # Start connection status checker
        connection_checker_task = asyncio.create_task(
            check_connection_status())

        # Start a periodic task to ensure conversations are saved
        async def periodic_conversation_saver():
            try:
                while True:
                    # Increased from 30 to 60 seconds to reduce storage
                    # frequency
                    await asyncio.sleep(60)  # Check every minute

                    # Skip if agent is actively speaking
                    if agent and (hasattr(agent, 'is_speaking') and agent.is_speaking or
                                 hasattr(agent, 'should_pause_background_tasks') and agent.should_pause_background_tasks()):
                        logger.debug(
                            "Skipping periodic save during active speech")
                        continue

                    if conversation_history:
                        unsaved_count = sum(
    1 for msg in conversation_history if not msg.get(
        "metadata", {}).get(
            "stored", False))
                        if unsaved_count > 0:
                            logger.info(
    f"Periodic save: Found {unsaved_count} unsaved messages")
                            # Instead of awaiting, create a low-priority
                            # background task
                            asyncio.create_task(store_full_conversation())
                        else:
                            logger.debug(
                                "Periodic save: All messages already saved")
            except asyncio.CancelledError:
                logger.info("Periodic conversation saver task cancelled")
            except Exception as e:
                logger.error(f"Error in periodic conversation saver: {e}")

        # Start periodic save task
        periodic_saver_task = asyncio.create_task(
            periodic_conversation_saver())
        logger.info("Started periodic conversation saver task")

        # Start periodic retry processor task
        retry_processor_task = asyncio.create_task(periodic_retry_processor())
        logger.info("Started periodic retry processor task")

        # Parse metadata safely
        try:
            metadata = json.loads(
    participant.metadata) if participant.metadata else {}
            logger.info(f"Parsed metadata: {metadata}")
        except json.JSONDecodeError as e:
            logger.error( 
                f"Failed to parse metadata: {str(e)}, using empty dict"
            )
            metadata = {}

        # Immediately extract location and time data, before doing anything
        # else
        logger.info("Extracting user location and time context...")
        user_context = {
            "location": {},
            "local_time": {}
        }

        # First try to get IP address - this is our primary source of location
        # data
        client_ip = extract_client_ip(participant)
        if client_ip:
            logger.info(f"Successfully extracted client IP: {client_ip}")
            # Get geolocation data from IP
            location_data = get_ip_location(client_ip)
            if location_data:
                logger.info(
                    f"Successfully got location data from IP: {json.dumps(location_data)}"
                )
                user_context["location"] = location_data

                # If we have a timezone from geolocation, get local time
                # immediately
                if location_data.get("timezone"):
                    logger.info(
                        f"Getting local time from IP timezone: {location_data.get('timezone')}"
                    )
                    user_context["local_time"] = get_local_time(
                        location_data.get("timezone"))
                    logger.info(
                        f"Local time determined: {json.dumps(user_context['local_time'])}"
                    )
            else:
                logger.warning("Could not determine location from IP address")
        else:
            logger.warning("Could not extract client IP address")

        # Check if client directly provided location data in metadata (higher
        # priority than IP)
        if metadata.get("location"):
            logger.info(
                f"Client provided location data: {json.dumps(metadata.get('location'))}"
            )
            user_context["location"].update(metadata.get("location"))

        # Check if client directly provided timezone or local time data
        if metadata.get("timezone"):
            logger.info(
                f"Client provided timezone: { metadata.get('timezone')}"
            )
            timezone = metadata.get("timezone")
            user_context["local_time"].update(get_local_time(timezone))

        if metadata.get("local_time"):
            logger.info(
                f"Client provided local time: {metadata.get('local_time')}"
            )
            if "local_time" not in user_context:
                user_context["local_time"] = {}
            user_context["local_time"]["local_time"] = metadata.get(
                "local_time")
            # Try to infer time of day from provided time if possible
            try:
                time_str = metadata.get("local_time")
                if ":" in time_str:  # Simple check for time format
                    hour_part = time_str.split(":")[0]
                    if "T" in hour_part:  # ISO format like 2023-01-01T14:30:00
                        hour_part = hour_part.split("T")[1]
                    hour = int(hour_part.strip())

                    if 5 <= hour < 12:
                        user_context["local_time"]["time_of_day"] = "morning"
                    elif 12 <= hour < 17:
                        user_context["local_time"]["time_of_day"] = "afternoon"
                    elif 17 <= hour < 22:
                        user_context["local_time"]["time_of_day"] = "evening"
                    else:
                        user_context["local_time"]["time_of_day"] = "night"

                    user_context["local_time"]["is_business_hours"] = 9 <= hour < 17
                    logger.info(
                        f"Inferred time of day: {user_context['local_time']['time_of_day']}"
                    )
            except Exception as e:
                logger.warning(
                    f"Could not parse time of day from provided local_time: {str(e)}"
                )

        # Log what we found
        if user_context["location"]:
            location_str = ", ".join(
    f"{k}: {v}" for k,
     v in user_context["location"].items() if v)
            logger.info(f"Final user location context: {location_str}")
        else:
            logger.warning("No location context available")

        if user_context["local_time"]:
            time_str = ", ".join(
    f"{k}: {v}" for k,
     v in user_context["local_time"].items() if v)
            logger.info(f"Final user local time context: {time_str}")
        else:
            logger.warning("No local time context available")

        # Immediately store this context in a system message that will be saved
        # to the database
        initial_context_message = {
            "role": "system",
            "content": "User context information collected at session start",
            "timestamp": get_utc_now().isoformat(),
            "metadata": {
                "type": "session_start_context",
                "user_location": user_context.get("location", {}),
                "user_local_time": user_context.get("local_time", {})
            }
        }

        # Keep track of conversation history
        conversation_history = []
        conversation_history.append(initial_context_message)

        # Immediately store the initial context to database to ensure we capture location data
        # Even if the session ends prematurely
        try:
            # Store context message in database immediately
            await store_conversation_message(
                session_id=session_id,
                participant_id="system",
                conversation=initial_context_message
            )
            logger.info(
                "Successfully stored user location context in database immediately")
        except Exception as e:
            logger.error(f"Failed to store initial location context: {str(e)}")
            logger.error(f"Error type: {type(e)}")
            logger.error(f"Full error details: {repr(e)}")

        # Load the base prompt
        base_prompt = prompt.prompt

        # Prepare context strings
        location_context_str = "No specific location context available."
        if user_context["location"]:
            loc = user_context["location"]
            parts = [loc.get('city'), loc.get('region'), loc.get('country')]
            location_context_str = (
                f"User location: {', '.join(filter(None, parts))}"
            )


        time_context_str = "No specific time context available."
        if user_context["local_time"]:
            time_data = user_context["local_time"]
            parts = [
                f"Local time: {time_data.get('local_time', 'Unknown')}",
                f"Timezone: {time_data.get('timezone', 'Unknown')}",
                f"Time of day: {time_data.get('time_of_day', 'Unknown')}"
            ]
            time_context_str = f"User time: {'; '.join(parts)}"

        # Inject context into the prompt
        system_instructions = base_prompt.replace(
    "{{LOCATION_CONTEXT}}", location_context_str)
        system_instructions = system_instructions.replace(
            "{{TIME_CONTEXT}}", time_context_str)
        logger.info("Injected user context into system prompt.")

        # Initialize the Multimodal Agent with RealtimeModel
        global agent
        try:
            logger.info(
                "Attempting to create MultimodalAgent with RealtimeModel")
            agent = multimodal.MultimodalAgent(
                model=google.beta.realtime.RealtimeModel(
                    instructions=system_instructions,
                    voice="Puck",  # Or another desired voice
                    temperature=0.9,  # Higher temperature for more conversational, varied responses
                    api_key=os.environ.get(
                        "GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"),
                    # Start with audio, can add "TEXT" if needed
                    modalities=["AUDIO"]
                )
            )
            # Add is_speaking attribute for coordination
            agent.is_speaking = False

            # Add a lightweight method to check if the agent should pause
            # background tasks
            agent.should_pause_background_tasks = lambda: agent.is_speaking

            logger.info(
                "Successfully created MultimodalAgent with is_speaking attribute.")

            # Update last_message_time when user speaks
            # Event provides transcript string directly for MultimodalAgent
            def on_user_speech_committed(transcript: str):
                try:
                    global user_message  # Use global variable

                    # Skip minimal processing if speaking to avoid
                    # interruptions
                    if agent.is_speaking:
                        logger.debug(
                            "Received user speech while agent is speaking, deferring processing")

                    # Track message time
                    message_time = asyncio.get_event_loop().time()

                    # Reduce logging during conversation
                    logger.debug(
                        f"User speech committed: {transcript[:30]}...")

                    # Update the user_message variable (potentially for other
                    # uses)
                    user_message = transcript

                    # Try to extract user email from metadata if available
                    user_email = ""
                    try:
                        if participant and participant.metadata:
                            metadata = json.loads(
    participant.metadata) if isinstance(
        participant.metadata,
         str) else participant.metadata
                            if isinstance(
    metadata, dict) and "user_email" in metadata:
                                user_email = metadata.get("user_email", "")
                                logger.info(
    f"Found user email in metadata: {user_email}")
                    except Exception as email_err:
                        logger.warning(
    f"Could not extract user email: {email_err}")

                    # Create complete user message with LiveKit context
                    user_chat_message = {
                        "role": "user",
                        "content": transcript,
                        "timestamp": get_current_timestamp(),
                        "message_id": str(uuid.uuid4()),
                        "metadata": {
                            "type": "user_speech",
                            "stored": False,
                            "room_name": room_name,
                            "room_sid": room_sid,
                            "participant_identity": participant_identity,
                            "participant_sid": participant_sid,
                    "session_id": session_id,
                            "user_email": user_email
                        }
                    }

                    # Only add additional context data when not speaking to
                    # reduce processing
                    if not agent.is_speaking:
                        user_chat_message["metadata"]["user_location"] = user_context.get(
                            "location", {})
                        user_chat_message["metadata"]["user_local_time"] = user_context.get(
                            "local_time", {})

                    # Add to conversation history
                    conversation_history.append(user_chat_message)

                    # Create a low-priority background task for storage
                    asyncio.create_task(
                        store_conversation_message(
                            session_id=session_id,
                            participant_id="user",
                            conversation=user_chat_message
                        )
                    )

                except Exception as e:
                    logger.error(
                        f"Error in user speech handler: {type(e).__name__}"
                    )

            # Register the handler with optimized processing
            user_speech_handler = agent.on(
    "user_speech_committed", on_user_speech_committed)
            logger.info("Registered optimized user speech handler")
        except Exception as e:
            logger.error(f"Fatal error creating MultimodalAgent: {e}")
            logger.error("Cannot proceed without a working agent.")
            # Depending on requirements, you might want to raise the exception
            # or attempt a fallback if one existed.
            raise e  # Stop execution if agent creation fails critically

        # --- Function Calling Integration ---
        # Enable function handlers for web search and knowledge base query
        try:
            # Check if the model or agent exposes a method for tool/function handling
            tool_handler_registry = None
            if hasattr(agent, 'register_function_handler'):  # Check agent first
                tool_handler_registry = agent
            elif hasattr(agent, 'model') and hasattr(agent.model, 'register_function_handler'):  # Check model
                tool_handler_registry = agent.model

            if tool_handler_registry:
                # Register web search handler
                tool_handler_registry.register_function_handler("search_web", handle_gemini_web_search)
                logger.info("Successfully registered function handler for web search")

                # Register knowledge base handler
                tool_handler_registry.register_function_handler("query_knowledge_base", handle_knowledge_base_query)
                logger.info("Successfully registered function handler for knowledge base queries")

                # Provide tool declarations
                if hasattr(tool_handler_registry, 'set_tools'):
                    tools = [
                        get_web_search_tool_declaration(),
                        get_knowledge_base_tool_declaration()
                    ]
                    tool_handler_registry.set_tools(tools)
                    logger.info("Successfully provided tool declarations to agent/model")
                else:
                    logger.warning("Could not set tools - API method not found")
            else:
                logger.warning("Could not find a method to register function handlers - tools may not be available")
        except Exception as e:
            logger.error(f"Error setting up function handlers: {e}")
            logger.warning("Agent will continue without tool functionality")
        # --- End Function Calling Section ---

        # Function to handle the actual web search when called by Gemini
        async def handle_gemini_web_search(
            search_query: str,
            include_location: bool = False):
                    """Handles the web search request triggered by Gemini function call."""
                    logger.info(f"Gemini requested web search for: {search_query}")

                    query_to_search = search_query
                    if include_location and user_context.get("location"):
                        loc = user_context["location"]
                        if loc.get("city") and loc.get("country"):
                            location_str = f" in {loc.get('city')}, {loc.get('country')}"

                            query_to_search += location_str
                            logger.info(f"Added location context: {location_str}")

                    # Perform the actual search
                    search_results = await perform_actual_search(query_to_search)

                    # Provide results back to Gemini
                    if search_results and not search_results.startswith(
                        "Unable") and not search_results.startswith("Web search failed"):
                        logger.info(
                            "Web search successful. Results logged. Returning result string conceptually.")

                        # Attempt to add to history as a system message
                        try:
                            if hasattr(agent, 'add_to_history'):  # Check if agent has this method
                                await agent.add_to_history(role="system", content=search_results)

                                # Add to conversation history
                                system_message = {
                                    "role": "system",
                                    "content": search_results,
                                    "timestamp": datetime.utcnow().isoformat(),
                                    "metadata": {"type": "web_search", "query": search_query}
                                }
                                conversation_history.append(system_message)

                                # Store full conversation to Supabase
                                await store_full_conversation()
                                logger.info("Added web search results to conversation history and stored to Supabase")
                            else:
                                logger.warning("MultimodalAgent may not support add_to_history directly. Results not added.")
                        except Exception as e:
                            logger.error(f"Failed to add search results to history: {e}")

                        return search_results
                    else:
                        logger.error(f"Web search failed or returned no results.")
                        return "Web search failed."

        # Function to handle the knowledge base query when called by Gemini
        async def handle_knowledge_base_query(query: str):
            """Handles the knowledge base query triggered by Gemini function call."""
            logger.info(f"Gemini requested knowledge base query for: {query}")

            # Query Pinecone
            kb_results = await query_pinecone_knowledge_base(query)

            # Provide results back to Gemini (Conceptual return similar to web search)
            if kb_results and not kb_results.startswith("Internal knowledge base is currently unavailable") \
                and not kb_results.startswith("Could not process query") \
                and not kb_results.startswith("No specific information found") \
                and not kb_results.startswith("An error occurred"):
                logger.info("Knowledge base query successful. Results logged. Returning result string conceptually.")

                # Attempt to add to history as a system message
                try:
                    if hasattr(agent, 'add_to_history'):
                        await agent.add_to_history(role="system", content=kb_results)
                        await store_full_conversation()
                        logger.info("Attempted to add knowledge base results to MultimodalAgent history.")
                    else:
                        logger.warning("MultimodalAgent may not support add_to_history directly. KB Results not added.")
                except Exception as e:
                    logger.error(f"Failed to add knowledge base results to history: {e}")

                return kb_results # Return this after the try/except block
            else:
                logger.info(f"Knowledge base query failed or returned no results: {kb_results}")

                # Optionally inform LLM that nothing was found
                try:
                    if hasattr(agent, 'add_to_history'):
                        await agent.add_to_history(role="system", content="The knowledge base query did not return relevant information.")
                    else:
                        logger.warning("MultimodalAgent may not support add_to_history directly. KB empty result not added.")
                except Exception as e:
                    logger.error(f"Failed to add KB empty result message to history: {e}")

                return "Knowledge base query did not return relevant information." # Return this after the try/except block

        # Add the following code after the agent is created and before agent.start(ctx.room)
        # For example, after the function calling setup section

        # Register event handlers for message storage
        try:
            # Try multiple events for assistant responses
            @agent.on("assistant_response")
            def on_assistant_response(msg: llm.ChatMessage):
                try:
                    logger.info(f"ASSISTANT RESPONSE EVENT: {msg.content}")
                    if isinstance(msg.content, list):
                        msg_content = "\n".join(str(item) for item in msg.content)
                    else:
                        msg_content = msg.content

                    asyncio.create_task(store_assistant_message(msg_content, "assistant_response"))
                except Exception as e:
                    logger.error(f"Error in assistant_response callback: {str(e)}")
                    logger.error(f"Error type: {type(e)}")
                    logger.error(f"Full error details: {repr(e)}")

            @agent.on("message_sent")
            def on_message_sent(msg: Any):
                try:
                    logger.info(f"MESSAGE SENT EVENT: {msg}")

                    # Check if this is an assistant message
                    if hasattr(msg, 'role') and msg.role == "assistant":
                        if hasattr(msg, 'content'):
                            if isinstance(msg.content, list):
                                msg_content = "\n".join(str(item) for item in msg.content)
                            else:
                                msg_content = msg.content

                            asyncio.create_task(store_assistant_message(msg_content, "message_sent"))
                except Exception as e:
                    logger.error(f"Error in message_sent handler: {str(e)}")
                    logger.error(f"Error type: {type(e)}")
                    logger.error(f"Full error details: {repr(e)}")

            @agent.on("llm_response_complete")
            def on_llm_response_complete(msg: Any):
                try:
                    logger.info(f"LLM RESPONSE COMPLETE EVENT: {msg}")

                    # Check if this is an assistant message with content
                    if hasattr(msg, 'role') and msg.role == "assistant" and hasattr(msg, 'content'):
                        if isinstance(msg.content, list):
                            msg_content = "\n".join(str(item) for item in msg.content)
                        else:
                            msg_content = msg.content

                        asyncio.create_task(store_assistant_message(msg_content, "llm_response_complete"))
                except Exception as e:
                    logger.error(f"Error in llm_response_complete handler: {str(e)}")
                    logger.error(f"Error type: {type(e)}")
                    logger.error(f"Full error details: {repr(e)}")

            # Add a message handler to log all messages for debugging
            @agent.on("message")
            def on_message(msg: Any):
                try:
                    logger.info(f"MESSAGE EVENT RECEIVED: {msg}")
                    if hasattr(msg, 'content'):
                        logger.info(f"Message content: {msg.content}")
                    if hasattr(msg, 'role'):
                        logger.info(f"Message role: {msg.role}")

                        # If this is an assistant message that we haven't captured through other events
                        if msg.role == "assistant" and hasattr(msg, 'content'):
                            # Check if we already have this message in our history
                            msg_content = msg.content
                            if isinstance(msg_content, list):
                                msg_content = "\n".join(str(item) for item in msg_content)

                            asyncio.create_task(store_assistant_message(msg_content, "message"))
                except Exception as e:
                    logger.error(f"Error in message handler: {str(e)}")
                    logger.error(f"Error type: {type(e)}")
                    logger.error(f"Full error details: {repr(e)}")

            logger.info("Registered all message handlers for Supabase conversation storage")
        except Exception as e:
            logger.error(f"Failed to register message handlers: {str(e)}")
            logger.error(f"Error type: {type(e)}")
            logger.error(f"Full error details: {repr(e)}")

        # Start the agent
        agent.start(ctx.room)

        # Handle participant disconnection (e.g., when user clicks "end call" button)
        # Needs to be synchronous, launch async tasks internally
        def on_participant_disconnected_sync(participant: rtc.Participant):
            logger.info(f"Participant disconnected: {participant.identity}")
            logger.info("Frontend user ended the call - cleaning up resources")

            async def async_disconnect_tasks():
                # Create a complete session end message with full context
                session_end_message = {
            "role": "system",
                    "content": "Call ended by user via frontend",
                    "timestamp": get_current_timestamp(),
                    "message_id": str(uuid.uuid4()),
                    "metadata": {
                        "type": "session_end",
                        "reason": "user_ended",
                        "room_name": room_name,
                        "room_sid": room_sid,
                        "participant_identity": participant_identity,
                        "participant_sid": participant_sid,
                        "session_id": session_id,
                        "end_time": get_current_timestamp(),
                        "stored": False
                    }
                }
                conversation_history.append(session_end_message)

                # Store final session state and end message
                await store_conversation_message(
                    session_id=session_id,
                    participant_id="system",
                    conversation=session_end_message
                )

                # Store full conversation to ensure complete transcript
                await store_full_conversation()

                # Ensure all messages are properly stored
                await ensure_storage_completed()

                # Count any messages that failed to store for logging
                failed_messages = sum(1 for msg in conversation_history if not msg.get("metadata", {}).get("stored", False))
                if failed_messages > 0:
                    logger.warning(f"Disconnecting with {failed_messages} unstored messages")
                else:
                    logger.info("All conversation messages successfully stored to database")

                # Disconnect the room (this will happen automatically, but we make it explicit)
                try:
                    global timeout_task
                    if timeout_task and not timeout_task.done():
                        timeout_task.cancel()
                        logger.info("Cancelled timeout task")

                    # Give a moment for cleanup and then disconnect
                    await asyncio.sleep(1)
                    await ctx.room.disconnect()
                    logger.info("Room disconnected after user ended call")
                except Exception as e:
                    logger.error(f"Error during room disconnect after user ended call: {str(e)}")

            # Launch async tasks from synchronous handler
            asyncio.create_task(async_disconnect_tasks())

        # Register the synchronous handler directly and store the reference
        participant_disconnect_handler = ctx.room.on("participant_disconnected", on_participant_disconnected_sync)
        logger.info("Registered participant disconnection handler")

        # Optimize speech handling to minimize interruptions
        async def say_and_store(message_text):
            try:
                # Create a message with complete LiveKit context
                assistant_message = {
                    "role": "assistant",
                    "content": message_text,
                    "timestamp": get_current_timestamp(),
                    "message_id": str(uuid.uuid4()),
                    "metadata": {
                        "type": "agent_speech",
                        "room_name": room_name,
                        "room_sid": room_sid,
                        "participant_identity": participant_identity,
                        "participant_sid": participant_sid,
                        "session_id": session_id,
                        "user_location": user_context.get("location", {}),
                        "user_local_time": user_context.get("local_time", {})
                    }
                }

                logger.info(f"Preparing to speak: {message_text[:50]}...")

                # First, prepare the audio - this caches the audio before playback begins
                # This helps reduce the delay between transcript and audio
                try:
                    preparation_task = asyncio.create_task(agent.prepare_say(message_text))

                    # Wait for audio preparation to complete (or timeout after 5 seconds)
                    try:
                        await asyncio.wait_for(preparation_task, timeout=5.0)
                        logger.info(f"Audio prepared and ready for playback")
                    except asyncio.TimeoutError:
                        logger.warning(f"Audio preparation timed out, proceeding anyway")
                except Exception as e:
                    logger.warning(f"Audio preparation not supported or failed: {str(e)}")

                # Now add to conversation history right before speaking
                # This ensures transcription appears at roughly the same time as audio
                conversation_history.append(assistant_message)

                # Create a background task for storage instead of awaiting it
                # This prevents blocking the speech
                storage_task = asyncio.create_task(store_conversation_message(
                    session_id=session_id,
                    participant_id="assistant",
                    conversation=assistant_message
                ))

                # Now begin speaking (speech and transcript should be closely synchronized)
                try:
                    # Set a speaking flag before starting speech
                    if hasattr(agent, 'is_speaking'):
                        agent.is_speaking = True

                    await agent.say(message_text, allow_interruptions=True)
                    logger.info("Speech completed successfully (agent.say returned).")

                    # Reset speaking flag after speech completes
                    if hasattr(agent, 'is_speaking'):
                        agent.is_speaking = False

                except asyncio.TimeoutError:
                    logger.error("Timeout occurred during agent.say")
                    # Reset speaking flag on error
                    if hasattr(agent, 'is_speaking'):
                        agent.is_speaking = False
                except Exception as say_e:
                    logger.error(f"Error during agent.say: {say_e}")
                    logger.error(f"Error type: {type(say_e)}")
                    # Reset speaking flag on error
                    if hasattr(agent, 'is_speaking'):
                        agent.is_speaking = False

                logger.info("Speech completed")

                # Wait for storage to complete after speech is done
                try:
                    await asyncio.wait_for(storage_task, timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("Storage task timed out, continuing anyway")

            except Exception as e:
                logger.error(f"Error in say_and_store: {str(e)}")
                logger.error(f"Error type: {type(e)}")
                logger.error(f"Full error details: {repr(e)}")

                # Reset speaking flag in case of errors
                if hasattr(agent, 'is_speaking'):
                    agent.is_speaking = False

                # Ensure the message is still stored even if speaking fails
                if 'assistant_message' in locals() and assistant_message not in conversation_history:
                    conversation_history.append(assistant_message)
                    # Use create_task instead of awaiting
                    asyncio.create_task(store_conversation_message(
                        session_id=session_id,
                        participant_id="assistant",
                        conversation=assistant_message
                    ))
                    logger.error("Stored message despite speech error")

        # Send initial welcome message
        initial_message = "Hi there. I'm Prepzo. I help with career stuff like resumes, interviews, job hunting, and career changes. What's on your mind today? I'm here to listen and offer some practical ideas."

        # Use the optimized say_and_store method for the welcome message
        # This ensures that the transcript and speech are synchronized from the start
        await say_and_store(initial_message)

        # Ensure initial messages are stored
        await ensure_storage_completed()

    finally:
        # Cleanup tasks
        try:
            # Cancel any running tasks - safely handle task cancellation
            tasks_to_cancel = [
                ('timeout_task', timeout_task),
                ('periodic_saver_task', periodic_saver_task),
                ('retry_processor_task', retry_processor_task),
                ('connection_checker_task', connection_checker_task if 'connection_checker_task' in locals() else None)
            ]

            for task_name, task in tasks_to_cancel:
                if task and not task.done():
                    try:
                        task.cancel()
                        logger.info(f"Cancelled {task_name}")
                    except Exception as e:
                        logger.error(f"Error cancelling {task_name}: {e}")

            # Ensure storage completes without creating backups
            try:
                await ensure_storage_completed()
            except Exception as e:
                logger.error(f"Error during final storage: {e}")

            # Process any remaining items in the retry queue
            try:
                if retry_queue:
                    await process_retry_queue()
            except Exception as e:
                logger.error(f"Error processing retry queue during shutdown: {e}")

            # Final log for debugging
            logger.info(f"Agent shutdown complete, {len(conversation_history)} messages in conversation history")

        except Exception as e:
            logger.error(f"Error during agent shutdown: {e}")
            # Skip creating local backup to avoid serialization errors

# Initialize the OpenAI and Gemini APIs
def init_apis():
    global openai_client, gemini_initialized

    # Initialize OpenAI client
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if openai_api_key:
        try:
            openai_client = OpenAI(api_key=openai_api_key)
            logger.info("OpenAI client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize OpenAI client: {str(e)}")
    else:
        logger.warning("OPENAI_API_KEY not set, some functionality may be limited")

    # Initialize Gemini API
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if gemini_api_key:
        try:
            genai.configure(api_key=gemini_api_key)
            gemini_initialized = True
            logger.info("Gemini API initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Gemini API: {str(e)}")
    else:
        logger.warning("GEMINI_API_KEY not set, web search functionality may be limited")

def sync_init_supabase():
    """Synchronous wrapper for async Supabase initialization"""
    import asyncio
    import dotenv
    
    # Load .env files first
    dotenv.load_dotenv()
    
    # Get Supabase credentials
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    
    # Print debug info
    logger.error(f"Initial env vars - SUPABASE_URL: {supabase_url}")
    logger.error(f"Initial env vars - SUPABASE_SERVICE_ROLE_KEY exists: {bool(supabase_key)}")
    
    # Try to read from .env file directly for debugging
    # ... (existing code)
    
    # Create a new event loop for this function only
    try:
        # Use a new event loop that we explicitly manage
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(init_supabase())
        return result
    finally:
        # Always close the loop when done to avoid interference with the main loop
        loop.close()

# --- Pinecone Initialization and Querying --- #

def init_pinecone():
    """Initializes the Pinecone client and connects to the index."""
    global pinecone_client, pinecone_index
    pinecone_api_key = os.environ.get("PINECONE_API_KEY")
    if not pinecone_api_key:
        logger.error("PINECONE_API_KEY not set in environment variables. Knowledge base functionality disabled.")
        return False

    try:
        logger.info(f"Initializing Pinecone client...")
        pinecone_client = Pinecone(api_key=pinecone_api_key)

        # Correctly get the list of index names
        index_names = [index_info.name for index_info in pinecone_client.list_indexes()]
        logger.info(f"Available Pinecone indexes: {index_names}")

        # Check if index exists and connect
        if PINECONE_INDEX_NAME not in index_names:
            logger.error(f"Pinecone index '{PINECONE_INDEX_NAME}' does not exist. Knowledge base functionality disabled.")
            pinecone_client = None # Disable client if index missing
            return False

        logger.info(f"Connecting to Pinecone index: {PINECONE_INDEX_NAME}")
        pinecone_index = pinecone_client.Index(PINECONE_INDEX_NAME)
        logger.info(f"Successfully connected to Pinecone index '{PINECONE_INDEX_NAME}'.")
        # Optional: Log index stats
        try:
            stats = pinecone_index.describe_index_stats()
            logger.info(f"Pinecone index stats: {stats}")
        except Exception as stat_e:
            logger.warning(f"Could not retrieve Pinecone index stats: {stat_e}")

        return True
    except Exception as e:
        logger.error(f"Failed to initialize Pinecone: {str(e)}")
        pinecone_client = None # Ensure client is None on failure
        pinecone_index = None
        return False

def get_embedding(text: str, model: str = EMBEDDING_MODEL):
    """Generates embeddings for the given text using OpenAI."""
    if not openai_client:
        logger.error("OpenAI client not initialized. Cannot generate embeddings.")
        return None
    try:
        text = text.replace("\n", " ")
        response = openai_client.embeddings.create(input=[text], model=model)
        return response.data[0].embedding
    except Exception as e:
        logger.error(f"Failed to get embedding from OpenAI: {str(e)}")
        return None

async def query_pinecone_knowledge_base(query: str, top_k: int = 3):
    """Queries the Pinecone knowledge base and returns relevant text chunks."""
    if not pinecone_index:
        logger.warning("Pinecone index not available, skipping knowledge base query.")
        return "Internal knowledge base is currently unavailable."

    try:
        logger.info(f"Generating embedding for knowledge base query: {query[:50]}...")
        query_embedding = get_embedding(query)

        if not query_embedding:
            return "Could not process query for the knowledge base."

        logger.info(f"Querying Pinecone index '{PINECONE_INDEX_NAME}'...")
        results = pinecone_index.query(
            vector=query_embedding,
            top_k=top_k,
            include_metadata=True # Assuming metadata contains the text
        )

        if not results or not results.matches:
            logger.info("No relevant documents found in knowledge base.")
            return "No specific information found in the knowledge base for that query."

        # Format results
        context_str = "Found relevant information in the knowledge base:\n"
        for i, match in enumerate(results.matches):
            score = match.score
            text_chunk = match.metadata.get('text', '[No text found in metadata]') # Adjust metadata field if needed
            source = match.metadata.get('source', 'Unknown source') # Example: get source if available
            context_str += f"\n{i+1}. (Score: {score:.2f}) From {source}:\n{text_chunk}\n"

        logger.info(f"Returning {len(results.matches)} results from knowledge base.")
        return context_str.strip()

    except Exception as e:
        logger.error(f"Error querying Pinecone knowledge base: {str(e)}")
        return f"An error occurred while accessing the knowledge base: {str(e)}"

# --- End Pinecone --- #

# Initialize local backup directory - DISABLED to avoid file serialization issues
def init_local_backup():
    """This function is disabled to prevent serialization errors"""
    logger.info("Local backup functionality is disabled")
    return True

# Save retry queue to disk - DISABLED to avoid file serialization issues
def save_retry_queue():
    """This function is disabled to prevent serialization errors"""
    # Logging disabled to avoid log spam
    return True

# Add a message to retry queue
def add_to_retry_queue(session_id, participant_id, conversation):
    """Add a failed message to the retry queue (in-memory only)"""
    global retry_queue

    # Make a safe copy of the message data to avoid serialization issues
    try:
        # First try to extract only the essential data to avoid coroutines
        safe_message = {
            "role": conversation.get("role", "unknown"),
            "content": conversation.get("content", ""),
            "timestamp": conversation.get("timestamp", get_current_timestamp()),
            "message_id": conversation.get("message_id", str(uuid.uuid4()))
        }

        # Create a retry item that matches the Supabase table structure
        retry_item = {
            "retry_timestamp": datetime.now().isoformat(),
            "session_id": session_id,
            "participant_id": participant_id,
            "conversation": safe_message,  # Store the actual conversation data
            "raw_conversation": safe_message.get("content", ""),
            "message_count": 1,
            "user_email": "",
            "timestamp": safe_message.get("timestamp"),
            "email_sent": False,
            "retry_count": 0
        }

        # Add to queue and trim if too large
        retry_queue.append(retry_item)
        if len(retry_queue) > MAX_RETRY_QUEUE_SIZE:
            retry_queue = retry_queue[-MAX_RETRY_QUEUE_SIZE:]  # Keep only the newest messages

        logger.info(f"Added message to retry queue (queue size: {len(retry_queue)})")
        return True
    except Exception as e:
        logger.error(f"Failed to add message to retry queue: {e}")
        return False

# Add a dedicated retry processor with adjustable batch size
async def process_retry_queue(batch_size=10):
    """Process the retry queue to attempt to store failed messages"""
    global retry_queue

    if not retry_queue:
        return 0

    if not supabase:
        logger.warning("Cannot process retry queue: Supabase client is not initialized")
        return 0

    total_queue_size = len(retry_queue)
    logger.info(f"Processing retry queue with {total_queue_size} messages (batch size: {batch_size})")

    # Process in batches to avoid overwhelming the database
    processed_count = 0
    success_count = 0

    # Process only up to batch_size items per run to limit impact
    items_to_process = min(batch_size, len(retry_queue))
    temp_queue = retry_queue[:items_to_process]

    # Remove processed items from the queue
    retry_queue = retry_queue[items_to_process:]

    for item in temp_queue:
        processed_count += 1

        # Skip items that have been retried too many times
        if item.get("retry_count", 0) >= 5:
            logger.warning(f"Skipping message that has failed {item['retry_count']} times")
            continue

        # Attempt to store the message directly to Supabase
        try:
            # Check Supabase health synchronously before attempting storage
            supabase_healthy = await check_supabase_health()

            if not supabase_healthy:
                logger.warning("Supabase connection not healthy, adding back to retry queue")
                item["retry_count"] = item.get("retry_count", 0) + 1
                retry_queue.append(item)
                continue

            # Prepare the item for direct storage to match Supabase table structure
            conversation_data = item.get("conversation", {})
            if not isinstance(conversation_data, str):
                conversation_data = json.dumps(conversation_data)

            insert_data = {
                "session_id": item.get("session_id"),
                "participant_id": item.get("participant_id"),
                "conversation": conversation_data,
                "raw_conversation": item.get("raw_conversation", ""),
                "message_count": item.get("message_count", 1),
                "user_email": item.get("user_email", ""),
                "timestamp": item.get("timestamp", get_current_timestamp()),
                "email_sent": item.get("email_sent", False)
            }

            # Insert directly to Supabase using proper structure
            response = (
                supabase.table("conversation_histories")
                .upsert([insert_data], on_conflict="session_id,timestamp")
                .execute()
            )

            if hasattr(response, 'data') and response.data:
                success_count += 1
                # Message was successfully stored, no need to add back to queue
                logger.info(f"Successfully stored retry item for session {item['session_id']}")
            else:
                # Message storage failed, increment retry count and add back to the queue
                logger.warning(f"Failed to store retry item: {response}")
                item["retry_count"] = item.get("retry_count", 0) + 1
                retry_queue.append(item)
        except Exception as e:
            logger.error(f"Error processing retry item: {e}")
            # Add back to queue with incremented retry count
            item["retry_count"] = item.get("retry_count", 0) + 1
            retry_queue.append(item)

        # Yield control back to the event loop more frequently
        await asyncio.sleep(0.2)

    logger.info(f"Retry queue processing: {success_count}/{processed_count} messages stored, {len(retry_queue)} remaining")

    return success_count

# Enhanced periodic retry task with reduced frequency and optimized background processing
async def periodic_retry_processor():
    """Periodically process the retry queue"""
    try:
        # Initial delay to avoid startup contention
        await asyncio.sleep(30)

        while True:
            # Increased from 60 to 180 seconds to reduce frequency
            await asyncio.sleep(180)  # Process retry queue every 3 minutes

            # Skip if queue is empty (most common case)
            if not retry_queue:
                continue

            # Skip if agent is actively speaking to avoid interruptions
            if agent and (hasattr(agent, 'is_speaking') and agent.is_speaking or
                         hasattr(agent, 'should_pause_background_tasks') and agent.should_pause_background_tasks()):
                logger.debug("Skipping retry processing during active speech")
                continue

            # Only process when supabase is connected and retry queue has items
            if await init_supabase():
                # Process a smaller batch size to reduce impact
                try:
                    await process_retry_queue(batch_size=5)
                except Exception as e:
                    logger.error(f"Error during periodic retry processing: {e}")
            else:
                logger.warning("Skipping retry processing due to Supabase connection issues")

    except asyncio.CancelledError:
        logger.info("Periodic retry processor task cancelled")
    except Exception as e:
        logger.error(f"Error in periodic retry processor: {e}")

# Custom exception for forced disconnection
class ForceDisconnectError(Exception):
    """Raised to force a disconnection in certain scenarios"""
    pass

# Helper function to store assistant message
async def store_assistant_message(msg_content: str, event_type: str):
    """Store an assistant message in the conversation history and Supabase."""
    try:
        # Check if this exact message is already in the conversation history
        is_duplicate = False
        for existing_msg in conversation_history:
            if (existing_msg.get('role') == 'assistant' and
                existing_msg.get('content') == msg_content):
                is_duplicate = True
                logger.info(f"Skipping duplicate assistant message from {event_type}")
                break

        if not is_duplicate:
            # Add to conversation history
            assistant_message = {
                "role": "assistant",
                "content": msg_content,
                "timestamp": datetime.utcnow().isoformat(),
                "metadata": {"type": "response", "event": event_type}
            }
            conversation_history.append(assistant_message)

            # Store updated conversation in Supabase
            await store_full_conversation()
            logger.info(f"Added assistant message to conversation history from {event_type}, total messages: {len(conversation_history)}")
    except Exception as e:
        logger.error(f"Error storing assistant message from {event_type}: {str(e)}")
        logger.error(f"Error type: {type(e)}")
        logger.error(f"Full error details: {repr(e)}")

# Add a helper method to directly await the storage task when critical
async def ensure_storage_completed():
    """Ensure that conversation storage is completed before proceeding."""
    try:
        await store_full_conversation()
        logger.info("Storage operation completed")
        return True
    except Exception as e:
        logger.error(f"Error during ensured storage: {str(e)}")
        return False

if __name__ == "__main__":
    # Create a synchronous wrapper that runs the async init in a new event loop
    # Run the initialization before starting the app
    if not sync_init_supabase():
        logger.error("Supabase initialization failed, exiting")
        import sys
        sys.exit(1)

    # Initialize APIs
    init_apis()

    # Initialize Pinecone
    if not init_pinecone():
        logger.warning("Pinecone initialization failed. Knowledge base functionality will be unavailable.")

    # Start health check HTTP server for deployment verification
    def start_health_check_server():
        import threading
        import http.server
        import socketserver
        import json
        from datetime import datetime
        import version

        PORT = int(os.environ.get("HEALTH_CHECK_PORT", 8080))

        class HealthCheckHandler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/health":
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()

                    # Get version info
                    ver_info = version.get_version_info()

                    # Prepare health check data
                    health_data = {
                        "status": "ok",
                        "service": "prepzo-bot",
                        "version": ver_info["version"],
                        "build_date": ver_info["build_date"],
                        "git_commit": ver_info["git_commit"],
                        "timestamp": datetime.now().isoformat(),
                        "uptime": time.time() - START_TIME,
                        "environment": os.environ.get(
                            "ENVIRONMENT", "production"
                        )
                    }

                    # Send response
                    self.wfile.write(json.dumps(health_data).encode())
                else:
                    self.send_response(404)
                    self.end_headers()

        httpd = socketserver.TCPServer(("", PORT), HealthCheckHandler)
        logger.info(f"Health check server started on port {PORT}")

        # Run server in a separate thread
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # Record start time for uptime calculation
    START_TIME = time.time()

    # Start health check server
    try:
        start_health_check_server()
        logger.info("Health check server started successfully")
    except Exception as e:
        logger.error(f"Failed to start health check server: {str(e)}")
        logger.error("Continuing without health check endpoint")

    # Standard worker options without async_init
    worker_options = WorkerOptions(
            entrypoint_fnc=entrypoint,
            worker_type=WorkerType.ROOM
        )
    # Start the app after successful initialization
    cli.run_app(worker_options)
