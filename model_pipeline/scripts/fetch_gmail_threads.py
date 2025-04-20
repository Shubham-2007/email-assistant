"""
Gmail Thread Fetcher Module

This module provides a Flask API server that processes Gmail thread data, generates
task-specific outputs (summaries, action items, and draft replies), and handles
user feedback for performance improvement.
"""

import os
import uuid
import time
import logging
from datetime import datetime

import mlflow
from flask import Flask, request, jsonify
from flask_cors import CORS
from google.cloud import logging as gcp_logging

from llm_generator import process_email_body
from llm_ranker import rank_all_outputs
from output_verifier import verify_all_outputs
from save_to_database import save_to_db
from update_database import update_user_feedback
from db_helpers import get_existing_user_feedback, get_last_3_feedbacks
from db_connection import get_db_connection
from initialize_db import initialize_all_tables
from config import (
    IN_CLOUD_RUN,
    GCP_PROJECT_ID,
    GMAIL_API_SECRET_ID,
    GMAIL_API_CREDENTIALS,
)
from mlflow_config import configure_mlflow
from config import MLFLOW_EXPERIMENT_NAME
from send_notification import send_email_notification
from monitoring_api import register_monitoring_endpoints

# Import secret manager if in Cloud Run
if IN_CLOUD_RUN:
    from secret_manager import get_credentials_from_secret


# Add this database initialization function
def create_tables_if_not_exists():
    """Initialize database tables if they don't exist."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Check if tables exist
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_name = 'user_feedback'
                    )
                """
                )
                row = cur.fetchone()
                if row is None:
                    logging.error("No result returned from table existence query")
                    raise Exception("Failed to check table existence")

                # Access the exists key from the RealDictRow
                tables_exist = row["exists"]

                logging.info(f"Table existence check result: {tables_exist}")

                if not tables_exist:
                    logging.info("Tables don't exist. Creating them...")
                    # Initialize all tables using imported functions
                    success = initialize_all_tables()
                    if success:
                        logging.info("All tables created successfully.")
                    else:
                        logging.error("Error creating some tables.")
                else:
                    logging.info("Tables already exist. Skipping initialization.")
    except Exception as e:
        logging.error(f"Error initializing database: {e}")


# Initialize Flask application
app = Flask(__name__)

create_tables_if_not_exists()

# Set up GCP Cloud Logging
gcp_client = gcp_logging.Client(project=GCP_PROJECT_ID)
gcp_logger = gcp_client.logger("gmail_thread_fetcher")
logging.getLogger().setLevel(logging.DEBUG)  # Fallback for local development


def setup_mlflow():
    """
    Set up MLflow for tracking metrics and artifacts.

    Returns:
        str: MLflow experiment ID
    """
    # Set up MLflow
    experiment = configure_mlflow()  # Set tracking URI once

    # Try to set the experiment, fall back to default if it fails
    try:
        if experiment is None:
            # Create it if it doesn't exist
            experiment_id = mlflow.create_experiment(MLFLOW_EXPERIMENT_NAME)
        else:
            experiment_id = experiment.experiment_id

        gcp_logger.log_struct(
            {"message": f"Using MLflow experiment ID: {experiment_id}"},
            severity="INFO",
        )

        return experiment_id
    except Exception as e:
        experiment_id = None
        gcp_logger.log_struct(
            {"message": f"Failed to get MLflow experiment: {str(e)}, using default"},
            severity="WARNING",
        )
        return experiment_id


def determine_prompt_strategy(email, tasks, request_id=None):
    """
    Determine which prompt strategy to use for each task based on:
    1. User's personal strategy settings (if available)
    2. User's recent feedback history (if negative)

    Args:
        email (str): User email
        tasks (list): List of tasks to determine strategies for
        request_id (str, optional): Unique identifier for request correlation

    Returns:
        tuple: (prompt_strategy, strategy_sources, negative_examples_by_task)
    """
    prompt_strategy = {
        "summary": None,
        "action_items": None,
        "draft_reply": None,
    }
    strategy_sources = {}
    negative_examples_by_task = {}

    # Ensure user exists in strategies table
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Check if user exists in user_prompt_strategies table
            cur.execute(
                "SELECT COUNT(*) as count FROM user_prompt_strategies WHERE user_email = %s",
                (email,),
            )
            user_exists = cur.fetchone()["count"] > 0

            if not user_exists:
                # Insert new user with default strategies
                query = """
                    INSERT INTO user_prompt_strategies (
                        user_email, summary_strategy, action_items_strategy, 
                        draft_reply_strategy, last_updated
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (user_email) DO NOTHING
                """
                cur.execute(
                    query,
                    (
                        email,
                        "default",
                        "default",
                        "default",
                        datetime.now(),
                    ),
                )
                conn.commit()

                gcp_logger.log_struct(
                    {
                        "message": f"New user added to strategies table: {email}",
                        "request_id": request_id,
                        "user_email": email,
                    },
                    severity="INFO",
                )

    # Step 1: Get user-specific strategies from the database
    user_strategies = None
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Get the user-specific prompt strategies directly from database
            query = """
                SELECT 
                    summary_strategy,
                    action_items_strategy,
                    draft_reply_strategy
                FROM user_prompt_strategies
                WHERE user_email = %s
            """
            cur.execute(query, (email,))
            result = cur.fetchone()

            if result:
                user_strategies = {
                    "summary": result["summary_strategy"] or "default",
                    "action_items": result["action_items_strategy"] or "default",
                    "draft_reply": result["draft_reply_strategy"] or "default",
                }
            else:
                # Fallback to defaults if no record found (shouldn't happen after the above)
                user_strategies = {
                    "summary": "default",
                    "action_items": "default",
                    "draft_reply": "default",
                }

            gcp_logger.log_struct(
                {
                    "message": f"Retrieved strategies for {email}",
                    "request_id": request_id,
                    "user_email": email,
                    "strategies": user_strategies,
                },
                severity="INFO",
            )

    # Step 2: Check recent user feedback to potentially override
    for task in tasks:
        feedback_column = f"{task}_feedback"
        recent_feedbacks = get_last_3_feedbacks(email, feedback_column, task)
        negative_count = sum(1 for f in recent_feedbacks if f[2] == 0)

        # If user has multiple negative feedbacks, use alternate strategy regardless of user settings
        if negative_count >= 2:
            prompt_strategy[task] = "alternate"
            negative_examples_by_task[task] = [
                (f[0], f[1]) for f in recent_feedbacks if f[2] == 0
            ]
            strategy_sources[task] = "recent_feedback"

            gcp_logger.log_struct(
                {
                    "message": f"Using alternate strategy for task {task} due to recent negative feedback",
                    "request_id": request_id,
                    "user_email": email,
                    "task": task,
                    "negative_count": negative_count,
                },
                severity="INFO",
            )
        else:
            # Otherwise, use the user's configured strategy
            prompt_strategy[task] = user_strategies.get(task, "default")
            negative_examples_by_task[task] = []
            strategy_sources[task] = "user_configured"

            gcp_logger.log_struct(
                {
                    "message": f"Using user-configured strategy for task {task}: {prompt_strategy[task]}",
                    "request_id": request_id,
                    "user_email": email,
                    "task": task,
                },
                severity="INFO",
            )

    return prompt_strategy, strategy_sources, negative_examples_by_task


# Configure Flask app
app.logger.handlers = []
app.logger.propagate = False

# Configure CORS for Chrome extension
CORS(
    app,
    resources={
        r"/.*": {
            "origins": ["chrome-extension://*"],
            "methods": ["POST", "OPTIONS"],
            "allow_headers": ["Authorization", "Content-Type"],
            "supports_credentials": True,
        }
    },
)

# Load Gmail API credentials in Cloud Run
if IN_CLOUD_RUN:
    try:
        get_credentials_from_secret(
            GCP_PROJECT_ID, GMAIL_API_SECRET_ID, save_to_file=GMAIL_API_CREDENTIALS
        )
        gcp_logger.log_text("Gmail API credentials loaded", severity="INFO")
    except Exception as e:
        gcp_logger.log_struct(
            {"message": "Failed to load Gmail credentials", "error": str(e)},
            severity="ERROR",
        )
        send_email_notification(
            "Gmail API Credential Failure",
            f"Failed to load Gmail API credentials: {str(e)}",
        )


@app.route("/health", methods=["GET"])
def health_check():
    """
    Health check endpoint for uptime monitoring.

    Returns:
        tuple: (JSON response, HTTP status code)
    """
    gcp_logger.log_text("Health check requested", severity="INFO")
    return jsonify({"status": "healthy"}), 200


@app.route("/fetch_gmail_thread", methods=["POST"])
def fetch_gmail_thread():
    """
    Fetch email thread details for a given thread ID and process it with LLMs.

    Returns:
        tuple: (JSON response, HTTP status code)
    """
    experiment_id = setup_mlflow()
    request_id = str(uuid.uuid4())  # Unique ID for request correlation
    start_time = time.time()

    # Log request start
    gcp_logger.log_struct(
        {"message": "Received fetch_gmail_thread request", "request_id": request_id},
        severity="INFO",
    )

    # Start MLflow run for this endpoint
    with mlflow.start_run(
        experiment_id=experiment_id, run_name=f"fetch_gmail_thread_{request_id}"
    ):
        try:
            # Log the MLflow artifact URI
            artifact_uri = mlflow.get_artifact_uri()
            gcp_logger.log_text(f"Artifact URI: {artifact_uri}", severity="INFO")

            # Get request data
            data = request.get_json()

            # Validate required fields
            required_fields = [
                "userEmail",
                "messageId",
                "threadId",
                "messagesCount",
                "body",
            ]
            if not all(
                field in data and data[field] is not None for field in required_fields
            ):
                error_msg = f"Missing one/more required fields: {required_fields}"
                gcp_logger.log_struct(
                    {"message": error_msg, "request_id": request_id}, severity="ERROR"
                )
                mlflow.log_param("error", error_msg)
                return jsonify({"error": error_msg}), 400

            # Extract key data from request
            thread_id = data.get("threadId")
            email = data.get("userEmail")
            requested_tasks = data.get("tasks", [])

            # Log input parameters
            mlflow.log_params(
                {
                    "thread_id": thread_id,
                    "user_email": email,
                    "tasks": requested_tasks,
                    "messages_count": data.get("messagesCount"),
                }
            )
            mlflow.log_dict(data, "input_request.json")  # Save request payload

            gcp_logger.log_struct(
                {
                    "message": "Processing request",
                    "request_id": request_id,
                    "thread_id": thread_id,
                    "user_email": email,
                    "tasks": requested_tasks,
                },
                severity="DEBUG",
            )

            # Validate tasks
            if not requested_tasks:
                error_msg = "No task specified"
                gcp_logger.log_struct(
                    {"message": error_msg, "request_id": request_id}, severity="ERROR"
                )
                mlflow.log_param("error", error_msg)
                return jsonify({"error": error_msg}), 400

            # Check if we already have results for this thread
            existing_record = get_existing_user_feedback(
                email, thread_id, data["messagesCount"], requested_tasks
            )
            gcp_logger.log_struct(
                {
                    "message": "Checked existing feedback",
                    "request_id": request_id,
                    "existing_record": bool(existing_record),
                },
                severity="DEBUG",
            )

            results = {}
            task_regen_needed = {}

            # Determine which prompt strategy to use based on user history and settings
            prompt_strategy, strategy_sources, negative_examples_by_task = (
                determine_prompt_strategy(email, requested_tasks, request_id)
            )

            # Log the strategy determination
            gcp_logger.log_struct(
                {
                    "message": "Determined prompt strategies",
                    "request_id": request_id,
                    "user_email": email,
                    "strategies": prompt_strategy,
                    "strategy_sources": strategy_sources,
                },
                severity="DEBUG",
            )

            print(
                {
                    "message": "Determined prompt strategies",
                    "request_id": request_id,
                    "user_email": email,
                    "strategies": prompt_strategy,
                    "strategy_sources": strategy_sources,
                }
            )

            # Determine which tasks need regeneration vs. reuse of existing results
            for task in requested_tasks:
                output_column = task
                feedback_column = f"{task}_feedback"
                if existing_record:
                    output_value = existing_record.get(output_column)
                    feedback_value = existing_record.get(feedback_column)
                    if output_value and (feedback_value == 1 or feedback_value is None):
                        # Reuse existing output with positive/no feedback
                        results[task] = output_value
                        prompt_strategy[task] = "reused"
                        task_regen_needed[task] = False
                        gcp_logger.log_struct(
                            {
                                "message": f"Reusing output for task {task}",
                                "request_id": request_id,
                                "thread_id": thread_id,
                            },
                            severity="INFO",
                        )
                        continue
                task_regen_needed[task] = True

            # Process tasks requiring regeneration
            for task in requested_tasks:
                if not task_regen_needed.get(task):
                    continue

                gcp_logger.log_struct(
                    {
                        "message": f"Generating output for task {task}",
                        "request_id": request_id,
                        "thread_id": thread_id,
                    },
                    severity="INFO",
                )

                try:
                    # Get email body
                    body = data["body"]

                    # Step 1: Generate multiple candidate outputs
                    llm_generator_output = process_email_body(
                        body=body,
                        task=task,
                        user_email=email,
                        experiment_id=experiment_id,
                        prompt_strategy=prompt_strategy,
                        negative_examples=negative_examples_by_task.get(task, []),
                        request_id=request_id,
                    )

                    # Step 2: Rank the candidate outputs
                    llm_ranker_output = rank_all_outputs(
                        llm_outputs=llm_generator_output,
                        task=task,
                        body=body,
                        experiment_id=experiment_id,
                        request_id=request_id,
                    )

                    # Step 3: Verify and select the best output
                    best_output = verify_all_outputs(
                        ranked_outputs_dict=llm_ranker_output,
                        task=task,
                        body=body,
                        userEmail=email,
                        experiment_id=experiment_id,
                        request_id=request_id,
                    )

                    # Store the best output
                    results[task] = best_output

                    # Log task output as artifact
                    mlflow.log_dict({task: best_output}, f"{task}_output.json")
                except Exception as e:
                    error_msg = f"Error generating output for task '{task}': {str(e)}"
                    gcp_logger.log_struct(
                        {
                            "message": error_msg,
                            "request_id": request_id,
                            "thread_id": thread_id,
                        },
                        severity="ERROR",
                    )
                    mlflow.log_param(f"{task}_error", str(e))
                    send_email_notification(
                        "LLM Processing Failure", error_msg, request_id
                    )
                    return jsonify({"error": error_msg}), 500

            # Prepare data for database
            message_data = {
                "Message-ID": data["messageId"],
                "From": data["from"],
                "To": data["to"],
                "Subject": data["subject"],
                "Date": data["date"],
                "Body": data["body"],
                "MessagesCount": data["messagesCount"],
                "Thread_Id": thread_id,
                "User_Email": email,
                "Prompt_Strategy": prompt_strategy,
            }

            # Save to database
            try:
                all_tasks_reused = all(
                    not needed for needed in task_regen_needed.values()
                )
                if not all_tasks_reused:
                    # Save new data
                    table_docid = save_to_db(
                        message_data,
                        {
                            "Summary": results.get("summary"),
                            "Action_Items": results.get("action_items"),
                            "Draft_Reply": results.get("draft_reply"),
                        },
                    )
                elif existing_record:
                    # Reuse existing doc ID
                    table_docid = existing_record["id"]
                else:
                    table_docid = None

                gcp_logger.log_struct(
                    {
                        "message": "Saved to database",
                        "request_id": request_id,
                        "doc_id": table_docid,
                        "reused": all_tasks_reused,
                    },
                    severity="INFO",
                )
                mlflow.log_param("doc_id", table_docid)
            except Exception as e:
                error_msg = f"Error saving to database: {str(e)}"
                gcp_logger.log_struct(
                    {"message": error_msg, "request_id": request_id}, severity="ERROR"
                )
                mlflow.log_param("db_error", str(e))
                send_email_notification("Database Save Failure", error_msg, request_id)
                return jsonify({"error": error_msg}), 500

            # Prepare response
            response = {
                "threadId": thread_id,
                "userEmail": email,
                "docId": table_docid,
                "result": results,
                "promptStrategy": prompt_strategy,
                "strategySource": strategy_sources,
            }

            # Log response and duration
            duration = time.time() - start_time
            gcp_logger.log_struct(
                {
                    "message": "Request completed successfully",
                    "request_id": request_id,
                    "duration_seconds": duration,
                    "thread_id": thread_id,
                },
                severity="INFO",
            )
            mlflow.log_metric("request_duration_seconds", duration)
            mlflow.log_dict(response, "response.json")
            return jsonify(response)
        except Exception as e:
            error_msg = f"Unexpected server error: {str(e)}"
            gcp_logger.log_struct(
                {"message": error_msg, "request_id": request_id}, severity="ERROR"
            )
            mlflow.log_param("server_error", str(e))
            send_email_notification("Server Error", error_msg, request_id)
            return jsonify({"error": error_msg}), 500


@app.route("/store_feedback", methods=["POST"])
def store_feedback():
    """
    Store user feedback for a task.

    Handles thumbs up/down feedback on generated content and updates the database.

    Returns:
        tuple: (JSON response, HTTP status code)
    """
    experiment_id = setup_mlflow()
    request_id = str(uuid.uuid4())
    start_time = time.time()
    gcp_logger.log_struct(
        {"message": "Received store_feedback request", "request_id": request_id},
        severity="INFO",
    )

    with mlflow.start_run(
        experiment_id=experiment_id, run_name=f"store_feedback_{request_id}"
    ):
        try:
            # Get and validate request data
            data = request.get_json()
            required_fields = [
                "userEmail",
                "threadId",
                "task",
                "rating",
                "timestamp",
                "docId",
            ]

            # Check for missing JSON payload
            if not data:
                error_msg = "Missing JSON payload"
                gcp_logger.log_struct(
                    {"message": error_msg, "request_id": request_id}, severity="ERROR"
                )
                mlflow.log_param("error", error_msg)
                return jsonify({"error": error_msg}), 400

            # Check for missing required fields
            missing = [field for field in required_fields if field not in data]
            if missing:
                error_msg = f"Missing required fields: {', '.join(missing)}"
                gcp_logger.log_struct(
                    {"message": error_msg, "request_id": request_id}, severity="ERROR"
                )
                mlflow.log_param("error", error_msg)
                return jsonify({"error": error_msg}), 400

            # Extract key data
            task = data["task"]
            feedback = (
                0 if data["rating"] == "thumbs_down" else 1
            )  # Convert rating to binary
            doc_id = data["docId"]

            # Log parameters
            mlflow.log_params(
                {
                    "user_email": data["userEmail"],
                    "thread_id": data["threadId"],
                    "task": task,
                    "feedback": feedback,
                    "doc_id": doc_id,
                }
            )
            mlflow.log_dict(data, "feedback_request.json")

            gcp_logger.log_struct(
                {
                    "message": "Processing feedback",
                    "request_id": request_id,
                    "task": task,
                    "feedback": feedback,
                    "doc_id": doc_id,
                },
                severity="DEBUG",
            )

            # Map task names to database column names
            valid_tasks = {
                "summary": "summary_feedback",
                "action_items": "action_items_feedback",
                "draft_reply": "draft_reply_feedback",
            }
            column_name = valid_tasks.get(task)

            # Validate task type
            if not column_name:
                error_msg = f"Invalid task type: {task}"
                gcp_logger.log_struct(
                    {"message": error_msg, "request_id": request_id}, severity="ERROR"
                )
                mlflow.log_param("error", error_msg)
                return jsonify({"error": error_msg}), 400

            try:
                # Update the database with user feedback
                response = update_user_feedback(
                    column_name=column_name, feedback=feedback, doc_id=doc_id
                )

                gcp_logger.log_struct(
                    {
                        "message": "Feedback stored successfully",
                        "request_id": request_id,
                        "response": response["message"],
                    },
                    severity="INFO",
                )
                mlflow.log_dict(response, "feedback_response.json")
                duration = time.time() - start_time
                mlflow.log_metric("feedback_duration_seconds", duration)
                return jsonify(response)
            except Exception as db_error:
                error_msg = f"Database update failed: {str(db_error)}"
                gcp_logger.log_struct(
                    {"message": error_msg, "request_id": request_id}, severity="ERROR"
                )
                mlflow.log_param("db_error", str(db_error))
                send_email_notification(
                    "Database Update Failure", error_msg, request_id
                )
                return jsonify({"error": error_msg}), 500
        except Exception as e:
            error_msg = f"Unexpected server error in store_feedback: {str(e)}"
            gcp_logger.log_struct(
                {"message": error_msg, "request_id": request_id}, severity="ERROR"
            )
            mlflow.log_param("server_error", str(e))
            send_email_notification("Server Error", error_msg, request_id)
            return jsonify({"error": error_msg}), 500


# Register the monitoring endpoints
register_monitoring_endpoints(app)


if __name__ == "__main__":
    print("Starting server")
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
