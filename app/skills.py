"""Skill taxonomy shared by resume parsing and job matching.

Each skill: canonical name -> (category, aliases). Aliases are regexes, matched case-insensitively
as whole words (so "go" does not match "good", "java" does not match "javascript").
Add skills here; both the resume parser and the job matcher pick them up.
"""
from __future__ import annotations

import re
from functools import lru_cache

LANG, BACKEND, FRONTEND, AI, DATA, CLOUD, DEVOPS, DB, MOBILE, TOOLS, CONCEPT = (
    "language", "backend", "frontend", "ai", "data", "cloud", "devops", "database", "mobile", "tools", "concept")

SKILLS: dict[str, tuple[str, list[str]]] = {
    # languages
    "Python": (LANG, [r"python\d?"]),
    "Java": (LANG, [r"java(?!\s*script)", r"core java", r"java ?8", r"java ?17"]),
    "JavaScript": (LANG, [r"javascript", r"js", r"es6", r"ecmascript"]),
    "TypeScript": (LANG, [r"typescript", r"ts"]),
    "Go": (LANG, [r"golang", r"go(?= (?:lang|developer|engineer|programming))"]),
    "Rust": (LANG, [r"rust"]),
    "C++": (LANG, [r"c\+\+", r"cpp"]),
    "C#": (LANG, [r"c#", r"c sharp"]),
    "C": (LANG, [r"c(?= (?:programming|language))", r"embedded c"]),
    "Kotlin": (LANG, [r"kotlin"]),
    "Swift": (LANG, [r"swift"]),
    "PHP": (LANG, [r"php"]),
    "Ruby": (LANG, [r"ruby"]),
    "Scala": (LANG, [r"scala"]),
    "R": (LANG, [r"r(?= (?:programming|language|studio))"]),
    "SQL": (LANG, [r"sql", r"t-sql", r"pl/sql"]),
    "Bash": (LANG, [r"bash", r"shell scripting", r"shell script"]),
    # backend
    "Node.js": (BACKEND, [r"node\.?js", r"node"]),
    "Express.js": (BACKEND, [r"express(?:\.?js)?"]),
    "NestJS": (BACKEND, [r"nest\.?js"]),
    "Spring Boot": (BACKEND, [r"spring ?boot", r"spring framework", r"spring mvc", r"spring"]),
    "Hibernate/JPA": (BACKEND, [r"hibernate", r"jpa", r"spring data"]),
    "Django": (BACKEND, [r"django"]),
    "FastAPI": (BACKEND, [r"fast ?api"]),
    "Flask": (BACKEND, [r"flask"]),
    ".NET": (BACKEND, [r"\.net", r"dotnet", r"asp\.net", r"\.net core"]),
    "Ruby on Rails": (BACKEND, [r"rails", r"ruby on rails"]),
    "Laravel": (BACKEND, [r"laravel"]),
    "REST APIs": (BACKEND, [r"restful(?: apis?| services?)?", r"rest ?apis?", r"rest services?", r"web services?"]),
    "GraphQL": (BACKEND, [r"graphql"]),
    "gRPC": (BACKEND, [r"grpc"]),
    "Microservices": (BACKEND, [r"micro-?services?"]),
    "WebSockets": (BACKEND, [r"websockets?", r"socket\.io"]),
    "Authentication": (BACKEND, [r"oauth ?2?", r"jwt", r"spring security", r"sso", r"keycloak"]),
    # frontend
    "React": (FRONTEND, [r"react(?:\.?js)?"]),
    "Next.js": (FRONTEND, [r"next\.?js"]),
    "Angular": (FRONTEND, [r"angular(?:js)?"]),
    "Vue": (FRONTEND, [r"vue(?:\.?js)?"]),
    "HTML/CSS": (FRONTEND, [r"html5?", r"css3?", r"tailwind(?: ?css)?", r"sass"]),
    "Redux": (FRONTEND, [r"redux"]),
    # AI / ML
    "Generative AI": (AI, [r"gen ?ai", r"generative ai", r"generative artificial intelligence"]),
    "LLMs": (AI, [r"llms?", r"large language models?", r"gpt-?\d?", r"openai", r"claude", r"anthropic",
                  r"gemini", r"llama", r"mistral"]),
    "RAG": (AI, [r"rag", r"retrieval[- ]augmented(?: generation)?"]),
    "Vector Databases": (AI, [r"vector (?:db|database|store|search)s?", r"pinecone", r"weaviate", r"qdrant",
                              r"pgvector", r"chroma(?:db)?", r"milvus", r"faiss"]),
    "Embeddings": (AI, [r"embeddings?"]),
    "AI Agents": (AI, [r"ai agents?", r"agentic(?: ai)?", r"llm agents?", r"autonomous agents?",
                       r"multi-agent", r"copilots?"]),
    "LangChain": (AI, [r"langchain", r"langgraph"]),
    "LlamaIndex": (AI, [r"llama ?index"]),
    "Agent Frameworks": (AI, [r"crew ?ai", r"autogen", r"semantic kernel", r"agents? sdk"]),
    "MCP": (AI, [r"mcp", r"model context protocol"]),
    "Prompt Engineering": (AI, [r"prompt engineering", r"prompting", r"function calling", r"tool calling"]),
    "Fine-tuning": (AI, [r"fine-?tuning", r"lora", r"qlora", r"rlhf", r"peft"]),
    "Machine Learning": (AI, [r"machine learning", r"ml"]),
    "Deep Learning": (AI, [r"deep learning", r"neural networks?"]),
    "NLP": (AI, [r"nlp", r"natural language processing"]),
    "Computer Vision": (AI, [r"computer vision", r"opencv", r"image processing"]),
    "PyTorch": (AI, [r"pytorch", r"torch"]),
    "TensorFlow": (AI, [r"tensorflow", r"keras"]),
    "Hugging Face": (AI, [r"hugging ?face", r"transformers"]),
    "scikit-learn": (AI, [r"scikit-?learn", r"sklearn"]),
    "MLOps": (AI, [r"mlops", r"mlflow", r"kubeflow", r"sagemaker", r"vertex ai"]),
    "Azure AI": (AI, [r"azure ai(?: foundry)?", r"ai foundry", r"azure openai", r"azure ml", r"cognitive services"]),
    "Copilot Studio": (AI, [r"copilot studio"]),
    "Voice AI": (AI, [r"voice ai", r"twilio", r"vapi", r"speech[- ]to[- ]text", r"text[- ]to[- ]speech",
                      r"conversational ai", r"voice assistants?"]),
    # data
    "Pandas/NumPy": (DATA, [r"pandas", r"numpy"]),
    "Spark": (DATA, [r"spark", r"pyspark", r"databricks"]),
    "Airflow": (DATA, [r"airflow"]),
    "ETL": (DATA, [r"etl", r"elt", r"data pipelines?"]),
    "Snowflake": (DATA, [r"snowflake"]),
    "Web Scraping": (DATA, [r"web scraping", r"scraping", r"crawl(?:er|ers|ing)", r"playwright", r"selenium",
                            r"puppeteer", r"beautiful ?soup"]),
    # cloud
    "AWS": (CLOUD, [r"aws", r"amazon web services", r"ec2", r"s3", r"dynamodb", r"ecs", r"eks"]),
    "AWS Lambda": (CLOUD, [r"lambda", r"serverless"]),
    "AWS SQS/SNS": (CLOUD, [r"sqs", r"sns", r"eventbridge"]),
    "Azure": (CLOUD, [r"azure"]),
    "GCP": (CLOUD, [r"gcp", r"google cloud(?: platform)?", r"bigquery", r"cloud run"]),
    # devops
    "Docker": (DEVOPS, [r"docker", r"containeri[sz]ation", r"docker compose"]),
    "Kubernetes": (DEVOPS, [r"kubernetes", r"k8s", r"helm"]),
    "CI/CD": (DEVOPS, [r"ci/cd", r"ci cd", r"jenkins", r"github actions", r"gitlab ci", r"azure devops"]),
    "Terraform": (DEVOPS, [r"terraform", r"infrastructure as code", r"iac", r"cloudformation"]),
    "Linux": (DEVOPS, [r"linux", r"unix"]),
    "Kafka": (DEVOPS, [r"kafka"]),
    "RabbitMQ": (DEVOPS, [r"rabbitmq", r"amqp"]),
    "Monitoring": (DEVOPS, [r"prometheus", r"grafana", r"datadog", r"new relic", r"elk", r"observability"]),
    # databases
    "PostgreSQL": (DB, [r"postgres(?:ql)?"]),
    "MySQL": (DB, [r"mysql", r"mariadb"]),
    "MS SQL Server": (DB, [r"ms ?sql", r"sql server", r"mssql"]),
    "MongoDB": (DB, [r"mongo(?:db)?", r"mongoose"]),
    "Redis": (DB, [r"redis"]),
    "Elasticsearch": (DB, [r"elastic ?search", r"opensearch"]),
    "Cassandra": (DB, [r"cassandra"]),
    "Oracle": (DB, [r"oracle(?: db)?"]),
    # mobile
    "Android": (MOBILE, [r"android"]),
    "iOS": (MOBILE, [r"ios"]),
    "Flutter": (MOBILE, [r"flutter", r"dart"]),
    "React Native": (MOBILE, [r"react native"]),
    # tools / platforms
    "Git": (TOOLS, [r"git", r"github", r"gitlab", r"bitbucket"]),
    "Salesforce": (TOOLS, [r"salesforce", r"agentforce", r"apex"]),
    "Jira": (TOOLS, [r"jira"]),
    # concepts
    "System Design": (CONCEPT, [r"system design", r"distributed systems?", r"high availability"]),
    "Event-Driven Architecture": (CONCEPT, [r"event[- ]driven", r"message queues?", r"pub/?sub",
                                            r"message brokers?"]),
    "Design Patterns": (CONCEPT, [r"design patterns?", r"solid principles?", r"clean architecture"]),
    "Data Structures & Algorithms": (CONCEPT, [r"data structures?", r"algorithms?", r"dsa"]),
    "Unit Testing": (CONCEPT, [r"unit test(?:s|ing)?", r"jest", r"junit", r"pytest", r"mocha", r"tdd"]),
    "Agile": (CONCEPT, [r"agile", r"scrum"]),
}

BOUNDARY = r"(?<![\w.+#/-]){}(?![\w+#-])"


@lru_cache(maxsize=1)
def patterns() -> dict[str, re.Pattern]:
    return {name: re.compile("|".join(BOUNDARY.format(a) for a in aliases), re.I)
            for name, (_, aliases) in SKILLS.items()}


def category(name: str) -> str:
    return SKILLS[name][0]


def find_skills(text: str) -> dict[str, int]:
    """Skill -> number of mentions in text."""
    out: dict[str, int] = {}
    for name, pat in patterns().items():
        n = len(pat.findall(text))
        if n:
            out[name] = n
    return out
