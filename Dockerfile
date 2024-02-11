# Use an official Python runtime as the base image
FROM python:3.11-alpine

# Set the working directory in the container
WORKDIR /app

# Create a non-root user and group
RUN addgroup -S appuser && adduser -S -G appuser appuser

# Change ownership of the application code to the non-root user
RUN chown -R appuser:appuser /app

# Install git and any required dependencies
RUN apk update && apk add git

# Switch to the non-root user
USER appuser

# Copy the requirements.txt file into the container
COPY requirements.txt .

ENV MQTT_HOST=mqtt
ENV MQTT_PORT=1883
ENV SPA_HOST=intouch2

# Install the Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY main.py .

# Set the command to run your Python file
CMD ["python", "main.py"]