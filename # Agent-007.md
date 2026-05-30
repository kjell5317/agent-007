# Agent-007

This document sets the requirements for an agentic task planner.
Plan every step and ask back if something was unclear or missing. Suggest better options and flag issues early.
The application should be designed with focus on code quality, readability and maintainability. Every decision about architecture should keep in mind that there will be following features that have to build on top of this. Do never fail silently but always hand exceptions upwards with their origin to a notification service (introduced later).

## Deployment

The application should be deployable as a docker compose stack. Create one docker compose file to set up backend, frontend, database and an optional reverse proxy. Create a dev environment too.

## Backend

The backend should follow a router, service, repository pattern for different dommains. Use comments and clear function names. FastAPI with OpenAPI is recommended.

### Auth

The user should be able to login through Google OAuth. Set up a whitelist for emails and a list of scopes that the application gets API access from Google during the login. The application needs to handle different OAuth connections and should handle the token refreshing itself. The following endpoints are mandatory:

- GET oauth/{provider}/authorize
- GET oauth/{provider}/callback
- POST oauth/{provider}/disconnect
- GET auth/login
- POST auth/logout
- GET auth/user
- GET oauth/list # list all oauth providers

Google should be the only provider for login and additional ones are only for API usage

### Trigger

The application should have a scheduling service to provide triggers on different events. For example there could be cron jobs for different tasks, or an event needs to be triggered when a timestamp in the db is in the past. Also there should be different endpoints to trigger something:

- POST trigger/poll
- POST trigger/notification # this needs to accept different actions defined in an enum

### Ingestion

The Oauth providers link to APIs like Slack or Gmail where messsages can be pulled from. New messages should go through a per provider preproccessing to align the data of the different providers. Important attributes cover:

- sender (clear name)
- directed (if a message is directed at me or if I only received it as CC or in a groupchat)
- title
- content

these information should then be embedded. The information, the embedding, the thread_id (the ID of a gmail or slack thread, to bundle messages) and found links in the content should be permanently saved. Apply additional preprocessing or attributes if needed. The embeddings should be used to find similar information or tasks in the future. Therefore no IDs or other unique things should go into the embedding. The preprocessing should also cover removal of MD symbols, slack emojis or translation of user mentions to their names instead of IDs (useful?). Find a way to link direct messages together even if no shared thread id is available to prevent ingestion on messages like "Yes", if not feasible drop short or empty messages.
Ingestion should run in the background and should never block something from responding to the frontend.

### Agent

All actions should be traceable and non-blocking to other processes. The agents main task is to evaluate if there is a todo for me inside of an input. The implementation should focus on little token usage to be fast and cheap, while being reliable and accurate.

#### Task

A task is a todo for me it should have a few mandatory fields.

- label (defined list of labels)
- due_date
- estimation
- how much of it is doable by AI (0.0 - 1.0)
- confidence
- title
- description

optional

- location (home should be possible)
- url

#### Auto-Decider

If a new ingestion is almost completely similar to a previous one it should be dropped. It could always be possible that I get the same message through different providers. If similarity is high but not almost the same the inputs should be linked together and handed off to the follow-up path. If similarity is high to an input that is flagged with "no_task" the new input also should be "no_task". The Auto-decider should save time and costs by removing some agent invocations. Similarity search should be time weighted to make newer findings more important (factor editable through settings)

#### Follow-up Path

If a thread id or the auto decider links inputs together an agent has to evaluate the effect of the latest one to the previous one. Lock on similarity to not have two agents trying to manipulate the same input. For this path possibly three tools should be provided:

- no_change: the new input had no effect on the previous one. Try to minimize this outcome through preprocessing and thresholds of the auto-decider
- update the todo
- close the todo

in addition the agent is allowed to write a note to the db whenever it thinks that something could be useful in the future. These notes are also embedded and can be requested through a search tool. Also there should be the possibility to add more tools or mcp servers, like google calendar, notion or github. All external MCPs should have a tool allowlist so that not every tool is advertised.

#### new input path

Otherwise the agent has to decide between "no_task", "create" or "follow-up". No_task is dropped (but the information is kept, so similarity search can find this), create needs a type field and then creates a task or an event (more on that later) and follow-up hands over to a sub agent that applies the mentioned follow-up. Therefore this path has to provide the task id where it found a correlation. This path should have access to write and search notes (and additional MCPs) too.
A few words to the system prompt: Inputs will likely be related to github issues, I want the number being present. Events are not todos unless I need to register, prepare or buy something before. The current datetime should always be added to the prompt to make it easier for the agent to apply the ISO due_date.

#### knowledge

Also there has to be an POST /inputs endpoint where the user can send informational input or todos, the agent has to evaluate and extract required fields that were not given.
One last agent should be a chat that accepts questions that do a similarity search through notes and MCP and return the answer.
