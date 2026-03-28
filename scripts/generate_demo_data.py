"""
Dunder Mifflin Paper Co — Digital Transformation
Demo Data Generator for CaaS / smm-sync dashboard.

Creates a realistic .smm/ folder at the given path with:
  - 400 architectural decisions across 20 sprints (8 weeks)
  - ~50 contradictions detected
  - ~35 resolved, ~10 pending, ~5 dismissed
  - Full compliance_lineage.jsonl with SHA-256 hash chain
  - Realistic timestamps spread over 8 weeks

Usage:
    python scripts/generate_demo_data.py /tmp/dunder-mifflin

After running:
    cd /tmp/dunder-mifflin && smm dashboard
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────

PROJECT = "dunder-mifflin-digital"
EPOCH = datetime(2025, 9, 1, 9, 0, 0, tzinfo=timezone.utc)  # Sprint 1 start

MAKERS = ["claude-code", "cursor-agent", "lore-hook", "claude-code", "cursor-agent"]

# ── Sprint → PRD mapping ──────────────────────────────────────────────────────
SPRINT_PRD = {
    **{s: "PRD-001: Legacy Modernization" for s in range(1, 5)},
    **{s: "PRD-002: Cloud Migration" for s in range(5, 9)},
    **{s: "PRD-003: API Modernization" for s in range(9, 13)},
    **{s: "PRD-004: Security Hardening" for s in range(13, 17)},
    **{s: "PRD-005: Analytics Platform" for s in range(17, 21)},
}

FAKE_BRANCHES = [
    "feature/legacy-modernization", "feature/cloud-migration", "feature/api-redesign",
    "feature/security-hardening", "feature/analytics-platform", "feature/microservices",
    "feature/data-pipeline", "feature/auth-refactor", "feature/deployment-automation",
    "feature/monitoring-stack",
]
REVIEWERS = {
    "michael.scott": "VP Engineering",
    "jim.halpert": "Tech Lead",
    "dwight.schrute": "Security Lead",
    "pam.beesly": "Product Manager",
    "oscar.martinez": "Compliance Officer",
}

# ── Decision corpus ───────────────────────────────────────────────────────────
# Each entry: (title, type, rationale, alternatives, constraints, sprint)
# Sprint 1-indexed; each sprint is ~2.8 days of real calendar time

DECISION_CORPUS = [
    # ── Sprint 1-3: Legacy modernisation ────────────────────────────────────
    (
        "Migrate inventory database from Oracle to PostgreSQL",
        "architectural",
        "Oracle licensing costs $380k/year and blocks cloud migration. PostgreSQL has feature parity for our workload and is fully managed on AWS RDS. Performance benchmarks show < 5% degradation on our query patterns.",
        "MySQL 8, CockroachDB, Aurora",
        "Must retain full ACID guarantees; existing stored procedures must be ported",
        1,
    ),
    (
        "Replace SOAP supplier API with RESTful JSON API",
        "technical",
        "SOAP integration requires expensive middleware and slows partner onboarding from 6 weeks to 2 days with REST. All new suppliers only support REST. Existing SOAP clients will get a 12-month deprecation window.",
        "GraphQL, gRPC, keep SOAP with adapter",
        "Backward compatibility required for 3 legacy suppliers through Q1 2026",
        1,
    ),
    (
        "Decompose monolithic Java EAR application into microservices",
        "architectural",
        "The monolith has a 4-hour deployment cycle and a single team bottleneck. Decomposition enables independent deploy velocity per domain team. We'll start with the order management and inventory domains.",
        "Modular monolith, vertical slices, serverless rewrite",
        "Must not break existing EDI integrations during cutover",
        1,
    ),
    (
        "Migrate on-premises data center to AWS (us-east-1 primary)",
        "architectural",
        "On-prem lease expires March 2026 and renewal cost exceeds 3-year cloud TCO. AWS provides HA, DR, and compliance certifications we need for enterprise customers. Migration follows lift-and-shift then modernise.",
        "Azure, GCP, colocation, private cloud",
        "Data residency must remain in USA; HIPAA BAA required for healthcare clients",
        1,
    ),
    (
        "Replace manual CSV reports with automated BI dashboards",
        "product",
        "Finance team spends 40 hrs/week on Excel aggregation. Metabase on RDS gives self-serve analytics without a data warehouse build-out. Reduces reporting lag from T+3 to real-time.",
        "Tableau, Looker, Power BI, Redash",
        "Budget cap of $2k/month for tooling; must support SSO with Okta",
        1,
    ),
    (
        "Replace FTP file transfer with S3 event-driven ingestion",
        "technical",
        "FTP has no audit trail, no encryption in transit, and requires VPN. S3 with EventBridge gives us automatic processing triggers, versioning, and encryption at rest. Partner onboarding drops from days to hours.",
        "SFTP on EC2, MFT platform, Azure Blob",
        "Some partners only support FTP — provide S3-compatible SFTP gateway for them",
        1,
    ),
    (
        "Move business logic from stored procedures to application layer",
        "technical",
        "270 stored procedures in Oracle make schema migration impossible. Application-layer logic is testable, versionable, and language-agnostic. Will port incrementally alongside the DB migration.",
        "Keep in DB with PostgreSQL functions, use DBT for transforms",
        "Must not change API contracts for existing integrations during port",
        1,
    ),
    (
        "Containerise all services with Docker on Linux",
        "technical",
        "Windows Server 2012 licenses expire and are unsupported. Docker on Linux reduces per-instance cost by 60% and enables Kubernetes migration in Sprint 5. Dev-prod parity eliminates environment drift issues.",
        "Windows containers, VMs, bare metal Linux",
        "Legacy COM components in order service cannot be containerised — exclude from scope",
        1,
    ),
    (
        "Adopt trunk-based development and remove long-lived feature branches",
        "technical",
        "Current Gitflow model causes 3-day merge hell every sprint. Trunk-based development with feature flags enables continuous deployment. CI runs go from 45min to 8min with parallel pipelines.",
        "Gitflow, GitHub Flow, release branches only",
        "All developers must complete TBD training; feature flag tool must be selected first",
        1,
    ),
    (
        "Standardise on Python 3.12 for all new backend services",
        "technical",
        "Current stack spans Python 2.7, 3.8, and Node 14. Python 3.12 provides best async support for our IO-heavy workloads and the team has deepest expertise. Unifies dependency management under uv/pyproject.toml.",
        "Go, Node 18, keep polyglot, Java Spring Boot",
        "Document processing service must stay in Java until migration in Sprint 8",
        2,
    ),
    (
        "Use Alembic for database schema migrations",
        "technical",
        "Ad-hoc SQL scripts applied manually cause schema drift between environments. Alembic provides versioned, reversible migrations with autogenerate from SQLAlchemy models. Integrates into CI pipeline.",
        "Flyway, Liquibase, custom migration runner",
        "Must be compatible with both Oracle (legacy) and PostgreSQL (target) during migration period",
        2,
    ),
    (
        "Adopt OpenAPI 3.1 spec-first API design",
        "technical",
        "Current APIs have no documentation and contracts change without notice. Spec-first with OpenAPI enables auto-generated clients, mocking, and contract testing. Reduces integration bugs by enforcing shared contracts.",
        "RAML, AsyncAPI, document-after approach",
        "Existing APIs must be retroactively documented before any breaking changes",
        2,
    ),
    (
        "Use Pydantic v2 for request/response validation across all Python services",
        "technical",
        "Inconsistent validation across services causes silent data corruption. Pydantic v2 is 5-17x faster than v1 and provides strict mode for type safety. Centralises validation logic and error message formatting.",
        "marshmallow, attrs, dataclasses with manual validation",
        "Services using v1 must migrate by end of Sprint 4 to avoid dependency conflicts",
        2,
    ),
    (
        "Centralise secret management in AWS Secrets Manager",
        "technical",
        "Secrets currently hardcoded in config files checked into Git. Secrets Manager provides rotation, audit trail, and fine-grained IAM access. SAST scan found 47 hardcoded credentials in the repo.",
        "HashiCorp Vault, SSM Parameter Store, environment variables only",
        "Rotation must be zero-downtime; all services must retrieve secrets at startup, not embed them",
        2,
    ),
    (
        "Implement structured JSON logging with correlation IDs",
        "technical",
        "Current print-statement logs are unqueryable in production. JSON logs with correlation IDs enable distributed trace reconstruction in CloudWatch Logs Insights. Reduces MTTR from 4 hours to 20 minutes based on pilots.",
        "OpenTelemetry traces only, ELK stack, Splunk",
        "All existing services must add correlation ID middleware before observability migration in Sprint 6",
        2,
    ),
    (
        "Deploy PostgreSQL on AWS RDS Multi-AZ with read replicas",
        "technical",
        "Single-AZ Oracle had 3 unplanned outages last year totalling 11 hours. RDS Multi-AZ provides automatic failover < 60s. Two read replicas handle reporting workload to keep primary write throughput clean.",
        "Aurora PostgreSQL, self-managed EC2 PostgreSQL, CockroachDB",
        "RTO < 5 min, RPO < 30s per SLA commitments to enterprise customers",
        2,
    ),
    (
        "Use Celery with SQS for async task processing",
        "technical",
        "Synchronous document processing blocks HTTP request threads and causes timeouts. Celery with SQS provides reliable async execution with retry, dead-letter queues, and visibility timeout. Scales independently of web tier.",
        "AWS Lambda, Dramatiq, RQ, Huey",
        "Tasks must be idempotent; max task duration 10 minutes before SQS visibility timeout",
        2,
    ),
    (
        "Store all file assets in S3 with CloudFront CDN",
        "technical",
        "Files currently stored on NFS mounts that don't survive container restarts. S3 provides durable object storage with lifecycle policies. CloudFront reduces latency for the US East customer base by 65%.",
        "EFS, MinIO, Azure Blob, local disk with sync",
        "PDFs over 50MB must use S3 multipart upload; presigned URLs expire after 15 minutes",
        2,
    ),
    (
        "Implement rate limiting at API Gateway level",
        "technical",
        "Supplier APIs were DDoS'd last quarter causing 6-hour outage. API Gateway throttling protects backend services. Token bucket algorithm allows burst traffic while preventing sustained abuse.",
        "Application-level rate limiting, Nginx limit_req, WAF rules only",
        "Enterprise customers need higher rate limits — implement per-API-key tiers",
        2,
    ),
    (
        "Adopt semantic versioning for all internal APIs",
        "technical",
        "Breaking API changes cause silent failures in downstream services. SemVer with proper MAJOR bump policy and deprecation notices prevents uncoordinated breaking changes. APIs must maintain v-1 support for 90 days.",
        "Date-based versioning, URL versioning (/v1/, /v2/), header versioning",
        "All existing v0 APIs must be documented and versioned before Sprint 3 ends",
        2,
    ),
    (
        "Use AWS CloudFormation for infrastructure-as-code",
        "technical",
        "Manual AWS console changes are not reproducible and caused the March DR drill failure. CloudFormation stacks are version controlled and enable reliable environment cloning for staging and DR.",
        "Terraform, CDK, Pulumi, Ansible",
        "CloudFormation templates must be stored in same repo as application code",
        3,
    ),
    (
        "Implement blue-green deployments via CodeDeploy",
        "technical",
        "Current in-place deployments cause 2-5 minute downtime per service release. Blue-green with CodeDeploy enables zero-downtime deployments with instant rollback capability. Required for our 99.9% SLA.",
        "Rolling deployments, canary releases, Spinnaker",
        "Database migrations must be backward-compatible for the duration of the blue-green window",
        3,
    ),
    (
        "Add Redis 7 as caching layer for hot catalogue data",
        "technical",
        "PostgreSQL read replicas at 80% CPU during peak product search. Redis cache reduces DB read load by 70% in load tests. TTL of 5 minutes balances freshness with performance for catalogue data.",
        "Memcached, DynamoDB DAX, application-level cache, Varnish",
        "Cache invalidation strategy must be defined for all write paths before enabling",
        3,
    ),
    (
        "Migrate frontend from jQuery/JSP to React 18",
        "product",
        "JSP templates require full-stack deployment for UI changes, slowing the design team. React 18 with React Query enables independent frontend deployments and component reuse across products. Reduces UI iteration from days to hours.",
        "Vue 3, Angular 17, Svelte, HTMX + server-side rendering",
        "IE11 support must be maintained until Q1 2026 for 8% of enterprise users",
        3,
    ),
    (
        "Implement JWT-based authentication for internal service mesh",
        "technical",
        "Services currently trust network location for authentication. JWT with short expiry (15 min) and refresh tokens provides cryptographic identity verification. Eliminates lateral movement risk from compromised services.",
        "mTLS, API keys, Kerberos, Istio service mesh",
        "Token refresh must be transparent to callers; clock skew tolerance max 5 seconds",
        3,
    ),
    (
        "Add Datadog APM for performance monitoring",
        "technical",
        "No visibility into service latency after decomposition. Datadog APM with distributed tracing maps inter-service calls and identifies bottlenecks automatically. P99 latency SLOs will be defined per service in Sprint 4.",
        "New Relic, Dynatrace, self-hosted Jaeger + Grafana",
        "Datadog cost must stay under $8k/month; instrument top 10 services first",
        3,
    ),
    (
        "Enforce TLS 1.3 for all internal and external traffic",
        "technical",
        "TLS 1.0/1.1 traffic was flagged in PCI DSS scan last quarter. TLS 1.3 removes vulnerable cipher suites and reduces handshake latency. All internal services must present certificates from our internal CA.",
        "TLS 1.2 only, VPN tunnel for internal, certificate pinning",
        "Some legacy suppliers only support TLS 1.2 — maintain compatibility via ALB policy until Q2 2026",
        3,
    ),
    (
        "Use AWS SES for transactional email delivery",
        "technical",
        "Current SMTP relay via on-prem Exchange will not migrate to cloud. SES handles bounce management, complaint feedback loops, and suppression lists automatically. Cost is 80% less than SendGrid at our volume.",
        "SendGrid, Postmark, Mailgun, self-hosted Postfix",
        "Must implement DKIM, SPF, and DMARC before go-live to avoid spam filtering",
        3,
    ),
    (
        "Implement database connection pooling via PgBouncer",
        "technical",
        "Django ORM opens 1 connection per thread causing connection exhaustion under load. PgBouncer in transaction mode multiplexes 200 app connections onto 20 DB connections. Eliminates OOM crashes seen in load tests.",
        "AWS RDS Proxy, HikariCP, application-level pooling",
        "Must be deployed as sidecar per service; connection string changes require app restart",
        3,
    ),
    (
        "Adopt event sourcing for order state management",
        "architectural",
        "Current order table is updated in-place losing audit history required by finance. Event sourcing provides immutable event log, enables temporal queries, and supports event replay for bug investigation. Orders are the right bounded context to start.",
        "Change data capture with Debezium, audit log table, temporal tables",
        "Projection rebuilds must complete < 2 hours for DR recovery; event store is append-only",
        3,
    ),
    (
        "Use AWS CloudFront with WAF for edge security",
        "technical",
        "Web application exposed directly to internet without edge protection. CloudFront + WAF blocks OWASP Top 10 at edge before traffic hits origin. Geo-blocking reduces attack surface from non-operating regions.",
        "Cloudflare, Akamai, Nginx ModSecurity, no WAF",
        "WAF rules must not break existing API integrations — run in count mode for 2 weeks before blocking",
        3,
    ),
    # ── Sprint 4-8: New features + contradictions ────────────────────────────
    (
        "Adopt GraphQL for mobile application API",
        "technical",
        "REST API requires 7 round-trips for the mobile home screen. GraphQL reduces to 1 request with field selection. Apollo Server with persisted queries prevents over-fetching and enables aggressive caching for mobile clients.",
        "REST with BFF pattern, gRPC, OData",
        "REST API must remain available for web and supplier integrations; no deprecation before Sprint 12",
        4,
    ),
    (
        "Upgrade to OAuth 2.0 + SAML 2.0 for enterprise SSO",
        "technical",
        "Enterprise customers require SSO with their IdP (Okta, Azure AD, Ping). JWT-only auth cannot federate. OAuth 2.0 PKCE for mobile, SAML 2.0 for enterprise SP-initiated login. Replaces in-house auth for all human-facing surfaces.",
        "OpenID Connect only, LDAP federation, Kerberos",
        "Existing JWT sessions must remain valid for 30 days post-migration; no forced re-login",
        4,
    ),
    (
        "Migrate from Celery to AWS Lambda for document processing",
        "technical",
        "Celery workers idle 80% of time waiting for document uploads. Lambda scales to zero and costs 90% less at our burst pattern. Cold start < 800ms is acceptable for async document processing.",
        "AWS Fargate tasks, keep Celery with auto-scaling, Azure Functions",
        "Functions must be < 512MB memory and < 15 min execution; large PDFs need chunked processing",
        4,
    ),
    (
        "Implement DynamoDB for product catalogue search index",
        "technical",
        "PostgreSQL full-text search degrades above 2M products. DynamoDB single-table design with GSIs provides < 10ms P99 for catalogue lookups at any scale. ElasticSearch was too expensive to operate.",
        "ElasticSearch, Algolia, PostgreSQL with pg_trgm, RDS read replica dedicated to search",
        "DynamoDB item size limit 400KB — product descriptions must be truncated or stored in S3",
        4,
    ),
    (
        "Use Docker Compose for local development environments",
        "technical",
        "Developer onboarding takes 2 days due to manual environment setup. Docker Compose brings up full stack in 3 minutes and eliminates works-on-my-machine issues. Dev environments mirror production service topology.",
        "Vagrant, Nix, manual setup scripts, devcontainers",
        "Compose files must be kept in sync with Kubernetes manifests; drift checks in CI",
        4,
    ),
    (
        "Deploy Kubernetes (EKS) for container orchestration",
        "architectural",
        "Docker Compose cannot handle rolling deploys, auto-scaling, or health-based routing in production. EKS provides managed control plane, Cluster Autoscaler, and integrates with ALB Ingress. Replaces EC2 Auto Scaling Groups.",
        "ECS Fargate, Nomad, self-managed k8s, AWS App Runner",
        "Kubernetes manifests must be managed in GitOps repo; no kubectl apply from laptops",
        4,
    ),
    (
        "Replace Datadog with OpenTelemetry + Grafana Cloud",
        "technical",
        "Datadog bill hit $18k/month in Sprint 6, 2.25x over budget. OpenTelemetry collector is vendor-neutral; Grafana Cloud costs $400/month at our metric cardinality. Reduces observability spend by 97%.",
        "New Relic, keep Datadog with aggressive sampling, Prometheus only",
        "Existing Datadog dashboards must be recreated in Grafana before Datadog cancellation",
        5,
    ),
    (
        "Migrate React frontend to Next.js 14 with SSR",
        "technical",
        "React SPA has poor SEO for the public product catalogue. Next.js SSR improves LCP from 4.2s to 1.1s and enables static generation for catalogue pages. App Router replaces React Router for better data fetching patterns.",
        "Remix, Astro, Gatsby, keep React SPA with prerender",
        "Migration must be incremental via Next.js pages directory; full App Router migration in Sprint 9",
        5,
    ),
    (
        "Implement CQRS for order and inventory domains",
        "architectural",
        "Event sourcing write side has different scaling needs from read side. CQRS separates command handlers from query models enabling independent scaling. Read models are projections updated asynchronously from the event store.",
        "Traditional CRUD with event sourcing, hybrid CQRS only on read side",
        "Eventual consistency window must be < 500ms for order status queries; SLA documented",
        5,
    ),
    (
        "Use AWS EventBridge for cross-service event routing",
        "technical",
        "Point-to-point SQS queues between services create tight coupling and fan-out complexity. EventBridge schema registry enforces event contracts. Archive and replay enables re-processing without service restarts.",
        "Apache Kafka, SNS+SQS fanout, direct service calls, RabbitMQ",
        "Event schema changes must be backward-compatible for 90 days; breaking changes require new event type",
        5,
    ),
    (
        "Adopt Terraform for infrastructure-as-code replacing CloudFormation",
        "technical",
        "CloudFormation stack drift and 400-line YAML templates are slowing infrastructure delivery. Terraform HCL is more readable, supports multi-cloud, and has better module ecosystem. State stored in S3 with DynamoDB locking.",
        "AWS CDK, Pulumi, keep CloudFormation, Crossplane",
        "Existing CloudFormation stacks must be imported into Terraform state without resource recreation",
        5,
    ),
    (
        "Implement API gateway pattern with Kong",
        "architectural",
        "Each microservice independently handles auth, rate limiting, and logging creating inconsistency. Kong gateway centralises cross-cutting concerns. Plugin ecosystem covers OAuth, rate limiting, logging, and transforms.",
        "AWS API Gateway, Nginx with Lua, Envoy, Traefik",
        "Kong must run in DB-less mode for GitOps compatibility; declarative config in repo",
        5,
    ),
    (
        "Use Kafka for real-time event streaming between domains",
        "technical",
        "EventBridge has 24-hour retention and no consumer group semantics. Kafka on MSK provides 7-day retention, consumer groups, and compacted topics for state snapshots. Required for real-time inventory sync across warehouses.",
        "AWS Kinesis, RabbitMQ, keep EventBridge, Pulsar",
        "Kafka topics must have 3 partitions minimum; replication factor 3 for all production topics",
        6,
    ),
    (
        "Move product images to CloudFront with WebP conversion",
        "technical",
        "JPEG product images average 1.2MB causing slow mobile load times. CloudFront Lambda@Edge converts to WebP on-the-fly. Average image size drops to 180KB. PageSpeed Insights score improves from 42 to 78.",
        "imgix, Cloudinary, server-side conversion at upload, client-side lazy load only",
        "WebP conversion must preserve image quality rating ≥ 4.5/5 per design team review",
        6,
    ),
    (
        "Implement circuit breaker pattern with Resilience4j",
        "technical",
        "Slow supplier API caused cascade failure across 3 services in April. Circuit breakers prevent cascade failures by fast-failing when error rate exceeds threshold. Fallback to cached data maintains partial functionality.",
        "Retry-only approach, Polly (.NET), Hystrix (deprecated), timeout-only",
        "Circuit state changes must emit events for monitoring; fallback data staleness must be surfaced to users",
        6,
    ),
    (
        "Centralise configuration management with AWS AppConfig",
        "technical",
        "Environment-specific config is spread across 12 repos and SSM parameters with no change history. AppConfig provides versioned, validated config with deployment strategies. Rollback is instant.",
        "Consul, etcd, Spring Cloud Config, environment variables only",
        "Config changes must go through PR review; AppConfig deployment strategy must be linear with bake time",
        6,
    ),
    (
        "Implement Saga pattern for distributed transactions",
        "technical",
        "Order placement spans 4 services with no rollback mechanism on partial failure. Saga choreography uses compensating transactions to maintain consistency without distributed locking. Each step publishes domain events.",
        "Two-phase commit, outbox pattern only, keep distributed transactions with locking",
        "All compensating transactions must be idempotent; saga state persisted in DynamoDB",
        6,
    ),
    (
        "Adopt GitOps with ArgoCD for Kubernetes deployments",
        "technical",
        "Manual kubectl apply and Helm chart deployments have caused 3 production incidents from config drift. ArgoCD syncs cluster state from Git automatically. Drift detection alerts on manual changes. All deploys become auditable.",
        "Flux, Jenkins X, Spinnaker, Helm only with CI push",
        "GitOps repo must be separate from application code repo; RBAC limits who can merge",
        6,
    ),
    (
        "Use AWS Aurora Serverless v2 for variable-load databases",
        "technical",
        "Provisioned RDS instances are 90% idle during off-peak hours but must be sized for peak. Aurora Serverless v2 scales ACU from 0.5 to 128 with sub-second latency. Cost reduction 40% vs provisioned for variable workloads.",
        "RDS with auto-scaling storage only, DynamoDB for everything, keep provisioned RDS",
        "Serverless v2 requires Aurora-compatible PostgreSQL dialect — ORM must not use pg-specific extensions",
        6,
    ),
    (
        "Implement distributed tracing with OpenTelemetry SDK",
        "technical",
        "Correlation IDs alone cannot reconstruct call graphs across 15 services. OTEL spans with baggage propagation give full request lineage. Traces exported to Grafana Tempo; retention 30 days for compliance.",
        "AWS X-Ray SDK, Jaeger SDK, DataDog tracing (cost), manual correlation only",
        "OTEL SDK must not add more than 5ms latency to P99; async span export via background queue",
        7,
    ),
    (
        "Migrate order service from Celery to Kafka consumer groups",
        "technical",
        "Celery tasks for order processing don't provide ordering guarantees within a customer partition. Kafka consumer groups with partition-by-customer-id guarantee per-customer ordering. Dead-letter topic handles poison messages.",
        "SQS FIFO queues, keep Celery with ordering workaround, Kinesis",
        "Consumer lag alerting must be configured; max acceptable lag 30 seconds before PagerDuty alert",
        7,
    ),
    (
        "Implement feature flags with LaunchDarkly",
        "product",
        "Feature releases require code deploy to test with subset of users. LaunchDarkly enables runtime flag evaluation with user targeting, percentage rollouts, and kill switches. Decouples deploy from release.",
        "Custom feature flag service, Flagsmith, Unleash, env variables",
        "LaunchDarkly SDK must not block request thread; async evaluation with local cache",
        7,
    ),
    (
        "Add ElasticSearch for full-text search across orders and documents",
        "technical",
        "Customer service team searches 10M+ orders and PDFs with PostgreSQL LIKE queries causing slow table scans. ElasticSearch provides sub-100ms full-text search with highlighting and fuzzy matching.",
        "PostgreSQL FTS with pg_trgm, Algolia, Typesense, Meilisearch",
        "ElasticSearch cluster must have 3 nodes minimum; index must be rebuilt from source of truth on schema change",
        7,
    ),
    (
        "Implement webhook delivery system for partner integrations",
        "technical",
        "Partners currently poll our API every 60 seconds for order status. Webhook push reduces API calls 95% and gives sub-second notification latency. Retry with exponential backoff and signature verification (HMAC-SHA256).",
        "WebSockets, SSE, polling with better caching, SNS direct delivery",
        "Webhook endpoints must respond within 5 seconds; failed deliveries retry for 72 hours then dead-letter",
        7,
    ),
    (
        "Adopt Infrastructure Cost Allocation Tags on all AWS resources",
        "technical",
        "AWS bill is $45k/month with no breakdown by service or team. Mandatory tags (team, service, env, cost-centre) enable per-team showback. Terraform modules enforce tagging via policy-as-code.",
        "AWS Cost Explorer with account-per-team, manual spreadsheet allocation",
        "Untagged resources will be auto-stopped after 72-hour warning; exemptions need VP approval",
        7,
    ),
    (
        "Use AWS Cognito for customer identity management",
        "technical",
        "In-house auth stores passwords with bcrypt but lacks MFA, social login, and passwordless options enterprise customers demand. Cognito provides OIDC-compatible IdP with managed MFA, SMS OTP, and TOTP support.",
        "Auth0, Keycloak, Firebase Auth, keep in-house",
        "Existing users must migrate without password reset; Cognito import API handles bcrypt hash migration",
        8,
    ),
    (
        "Implement async email templating with MJML + SES",
        "technical",
        "Transactional emails are HTML strings concatenated in Python causing rendering inconsistencies. MJML compiles responsive email templates; SES template API handles personalisation. Template changes no longer require deploys.",
        "Jinja2 HTML templates, Sendgrid Dynamic Templates, Postmark",
        "Email templates must render correctly in Outlook 2019 — test with Email on Acid before deploy",
        8,
    ),
    (
        "Adopt Playwright for end-to-end testing replacing Selenium",
        "technical",
        "Selenium tests fail 30% of the time due to flaky timing. Playwright auto-waits eliminate explicit waits, runs in all modern browsers, and is 3x faster. Test parallelisation across 8 workers brings suite to 4 minutes.",
        "Cypress, TestCafe, WebDriverIO, keep Selenium with stability improvements",
        "Must maintain test coverage for IE11 scenarios — keep 5 critical Selenium tests for IE11 path only",
        8,
    ),
    (
        "Implement health check endpoints on all services",
        "technical",
        "Kubernetes liveness/readiness probes are not configured causing traffic to dead pods. Standardised /health/live and /health/ready endpoints per service. Ready probe checks DB connectivity and downstream dependencies.",
        "Custom probe scripts, TCP-only probes, process-level checks",
        "Health endpoint must respond < 50ms; must not trigger heavy operations or DB writes",
        8,
    ),
    # ── Sprint 9-12: AI integration + compliance ─────────────────────────────
    (
        "Integrate Claude API for automated document processing",
        "technical",
        "Manual document digitisation costs $180k/year in contractor hours. Claude 3.5 Sonnet processes invoices, purchase orders, and supplier contracts with 94% accuracy in POC. Reduces processing time from 3 days to 4 hours.",
        "OpenAI GPT-4o, Azure Document Intelligence, AWS Textract, manual process",
        "PHI and PII must be scrubbed before sending to Claude API; all prompts logged for compliance",
        9,
    ),
    (
        "Use Claude API for AI-powered test case generation",
        "technical",
        "QA team covers only 60% of code paths due to resource constraints. Claude generates unit and integration test cases from function signatures and docstrings. Increases coverage to 87% in pilot on payments service.",
        "OpenAI Codex, GitHub Copilot tests, manual test writing",
        "Generated tests must pass human review before merging; AI-generated tag added to all test files",
        9,
    ),
    (
        "Replace email notifications with event-driven webhooks",
        "technical",
        "Email notifications for order status have 12-minute average delivery latency. Webhooks deliver in < 1 second and enable partners to build real-time integrations. Email fallback retained for human notifications.",
        "SMS, Push notifications only, polling API, keep email",
        "Partners must register webhook URLs via API with HTTPS only; test endpoint required before activation",
        9,
    ),
    (
        "Migrate batch processing to real-time Kafka Streams",
        "technical",
        "Nightly batch jobs process 500k orders causing database contention for 4 hours overnight. Kafka Streams processes orders as they arrive; balance between real-time and micro-batch based on volume.",
        "Apache Flink, Spark Streaming, AWS Kinesis Analytics, keep batch with parallelisation",
        "Stream processing must handle exactly-once semantics; idempotency keys on all state updates",
        9,
    ),
    (
        "Store sensitive documents in MinIO on-premises cluster",
        "constraint",
        "Legal identified 3 categories of documents (employment contracts, legal filings, audit reports) that cannot reside on public cloud per regulatory counsel. MinIO on Kubernetes provides S3-compatible API while satisfying data sovereignty requirements.",
        "AWS S3 with customer-managed keys, Azure Stack, keep in S3 with strict IAM",
        "MinIO cluster must have 4 nodes minimum for erasure coding; DR to second data centre required",
        9,
    ),
    (
        "Implement hybrid cloud strategy for compliance-sensitive workloads",
        "architectural",
        "PCI DSS Level 1 certification and upcoming EU AI Act compliance require certain workloads to run on infrastructure we control end-to-end. Hybrid cloud runs cardholder data environment and AI inference on-prem while public cloud handles scale-out.",
        "Full public cloud with compliance controls, private cloud only, multi-cloud without on-prem",
        "On-prem infrastructure must achieve same availability SLAs as public cloud; 3-year CapEx approved",
        9,
    ),
    (
        "Add vector database (pgvector) for semantic document search",
        "technical",
        "Keyword search misses synonyms and conceptually related documents. pgvector extension on PostgreSQL provides semantic similarity search using Claude-generated embeddings. Co-locating with existing PostgreSQL reduces operational overhead.",
        "Pinecone, Weaviate, Qdrant, Chroma, standalone vector DB",
        "Embedding model must be deterministic for consistency; re-embed all documents when model changes",
        9,
    ),
    (
        "Implement PII detection and redaction pipeline",
        "technical",
        "Claude API calls may inadvertently send customer PII in document text. AWS Comprehend detects PII entities (names, addresses, SSNs, credit cards) in documents before AI processing. Redacted version sent to Claude; original retained encrypted.",
        "Custom regex-based redaction, Microsoft Presidio, manual review",
        "PII detection must achieve < 0.1% false negative rate on test dataset; tune before production",
        9,
    ),
    (
        "Use SOPS + age encryption for secrets in GitOps repo",
        "technical",
        "Kubernetes secrets in GitOps repo are base64-encoded (not encrypted). SOPS with age keys encrypts secrets at rest in Git. Decryption key held by ArgoCD service account only; team members cannot decrypt production secrets.",
        "Sealed Secrets, External Secrets Operator, Vault Agent Injector, plain SSM references",
        "Key rotation procedure must be documented; age key backup must be in HSM with 2-person rule",
        10,
    ),
    (
        "Implement AI model versioning and rollback",
        "technical",
        "Claude API model upgrades can silently change document extraction output format. Pinned model versions with explicit upgrade process. Model change requires re-validation on test corpus before production. Rollback < 10 minutes.",
        "Latest model always, auto-upgrade with monitoring, separate staging for model validation",
        "Model version pinned in configuration, not code; change goes through AppConfig deployment",
        10,
    ),
    (
        "Adopt EU AI Act Article 12 audit logging for AI systems",
        "constraint",
        "Digital transformation includes AI components that fall under EU AI Act high-risk category. Article 12 requires automated logging of AI system inputs, outputs, and decisions. Compliance deadline Q1 2026. Audit logs immutable, 5-year retention.",
        "Manual logging, lightweight event tracking, defer to post-go-live",
        "Audit logs must be tamper-evident (hash chain); GDPR right-to-erasure conflicts with immutability — legal guidance pending",
        10,
    ),
    (
        "Implement GDPR data subject request automation",
        "constraint",
        "GDPR DSARs currently fulfilled manually taking 15-25 days vs 30-day legal requirement. Automated data discovery across PostgreSQL, S3, and DynamoDB identifies all PII for a subject. Deletion pipeline handles right-to-erasure within 48 hours.",
        "Manual process with better tooling, BigID, OneTrust, offshore DSAR team",
        "Erasure must cascade to all derived tables, ML training sets, and audit log exports — legal review required",
        10,
    ),
    (
        "Use AWS Macie for S3 PII discovery and classification",
        "technical",
        "Unknown volume of PII in S3 buckets accumulated over 5 years of migration. Macie classifies objects with 99.7% precision for 80+ PII types. Findings feed into security dashboard and trigger automated remediation.",
        "Custom Comprehend pipeline, third-party DLP, manual sampling, Nightfall",
        "Macie runs in discovery mode first for 30 days before enabling enforcement actions",
        10,
    ),
    (
        "Implement row-level security in PostgreSQL for multi-tenant data",
        "technical",
        "All enterprise tenants share the same schema and application-level filtering has had 2 data leakage bugs. PostgreSQL RLS enforces isolation at the database level, eliminating application-level risk. Zero-cost and no schema changes needed.",
        "Separate schemas per tenant, separate DB per tenant, application-layer only",
        "RLS policies must be reviewed by security lead; performance impact must be benchmarked at 1000 tenants",
        10,
    ),
    (
        "Migrate CI/CD from Jenkins to GitHub Actions",
        "technical",
        "Jenkins requires dedicated ops team to maintain; last quarter had 3 outages from plugin conflicts. GitHub Actions is fully managed, integrates natively with PRs, and reduces CI maintenance overhead to near zero.",
        "GitLab CI (requires self-hosted), CircleCI, TeamCity, Buildkite",
        "Must migrate all 47 Jenkins pipelines before Jenkins EOL in Q3 2026; no new Jenkins pipelines allowed",
        11,
    ),
    (
        "Add Prometheus + Grafana for SLO monitoring",
        "technical",
        "OpenTelemetry metrics need a query layer for SLO alerting. Prometheus records SLI metrics; Grafana SLO plugin calculates error budgets. PagerDuty integration fires when error budget burn rate exceeds 2x.",
        "Datadog SLOs (cost), CloudWatch Metric Math, NewRelic SLM",
        "SLOs must be approved by product owners before alerting activates; grey period 2 weeks per service",
        11,
    ),
    (
        "Implement data mesh architecture for analytics",
        "architectural",
        "Central analytics team is bottleneck for all reporting. Data mesh gives domain teams ownership of their data products. Each domain exposes a standardised data contract; consumers self-serve via catalog. dbt on Redshift per domain.",
        "Centralised data warehouse, data lake on S3 only, Databricks platform",
        "Data product SLAs must be defined before domain team ownership transfer; 6-month transition period",
        11,
    ),
    (
        "Use AWS Lake Formation for data governance",
        "technical",
        "Redshift access control is managed with 400 manually maintained IAM policies. Lake Formation provides column-level security, data access auditing, and tag-based permission inheritance across all analytics data.",
        "Apache Ranger, manual IAM, Immuta, Collibra",
        "All existing Redshift permissions must be migrated to Lake Formation before manual policies are deleted",
        11,
    ),
    (
        "Implement chaos engineering with AWS Fault Injection Simulator",
        "technical",
        "System resilience is untested — disaster recovery plans exist on paper but haven't been run in 18 months. FIS experiments run controlled failures (AZ outage, latency injection, EC2 termination) in staging then production monthly.",
        "Gremlin, manual chaos tests, Netflix Chaos Monkey, no chaos testing",
        "All FIS experiments must have defined steady-state hypothesis and success criteria before running",
        11,
    ),
    (
        "Adopt FinOps practice with weekly cost reviews",
        "product",
        "AWS bill grew 35% last quarter without corresponding business growth. Weekly FinOps review with team leads provides accountability. Rightsizing recommendations from Compute Optimizer are actioned within 2 sprints.",
        "Monthly reviews only, automated rightsizing without review, separate cloud team ownership",
        "Cost anomaly alerts must trigger within 4 hours of detection; anomaly threshold 20% above baseline",
        11,
    ),
    (
        "Implement content delivery optimisation with Brotli compression",
        "technical",
        "API responses average 2.3KB uncompressed. Brotli compression reduces to 0.4KB, a 5.75x improvement vs 4.5x for gzip. Implemented at ALB level; no application changes needed. Reduces egress costs 20%.",
        "gzip only, client-side compression, no compression",
        "Brotli must fall back to gzip for clients that don't advertise br in Accept-Encoding",
        11,
    ),
    # ── Sprint 13-20: Scale + more contradictions ────────────────────────────
    (
        "Migrate from Kubernetes to AWS ECS Fargate for stateless services",
        "architectural",
        "Kubernetes cluster operational overhead consumes 0.8 FTE. ECS Fargate eliminates node management while providing equivalent container orchestration for our 23 stateless services. Cost analysis shows 22% reduction vs EKS node pools.",
        "Keep Kubernetes, GKE Autopilot, Fly.io, AWS App Runner",
        "Stateful services (Kafka, ElasticSearch) remain on EKS; migration is services-only",
        13,
    ),
    (
        "Replace GraphQL with gRPC for internal service communication",
        "technical",
        "GraphQL between internal services introduces schema resolution overhead without the client benefit. gRPC with proto3 is 3-7x faster for internal RPC, provides strong typing via protobuf, and enables bidirectional streaming.",
        "REST with OpenAPI, Thrift, Avro over Kafka, keep GraphQL everywhere",
        "GraphQL remains for public mobile API; gRPC is internal only. Proto files version-controlled in buf.build",
        13,
    ),
    (
        "Split monorepo into domain-aligned multi-repo structure",
        "architectural",
        "Single monorepo CI takes 45 minutes to run all tests blocking 8 teams. Multi-repo with shared libraries via private PyPI registry enables per-domain CI under 8 minutes. Teams own their deployment cadence.",
        "Nx monorepo with affected builds, Bazel, keep monorepo with better CI caching",
        "Shared library versioning policy must be defined; breaking library changes require 30-day migration window",
        13,
    ),
    (
        "Replace LaunchDarkly with in-house feature flag service",
        "product",
        "LaunchDarkly costs $36k/year and we only use 20% of its features. Custom service using PostgreSQL + Redis covers our use cases at $200/year infrastructure cost. Migrating 140 flags over 6 weeks.",
        "Flagsmith (self-hosted), Unleash, Growthbook, keep LaunchDarkly",
        "Custom service must support targeting rules, percentage rollouts, and SDK for Python and Next.js",
        13,
    ),
    (
        "Adopt Pulumi for infrastructure-as-code replacing Terraform",
        "technical",
        "Terraform HCL lacks abstraction for our complex multi-account setup. Pulumi uses Python (our primary language), enabling reuse of existing utilities and type checking. State management identical to Terraform via S3 backend.",
        "Keep Terraform, CDK, Crossplane, OpenTofu",
        "Existing Terraform state must be converted to Pulumi without resource recreation; test on non-prod first",
        14,
    ),
    (
        "Migrate from GitHub Actions to GitLab CI (self-hosted)",
        "technical",
        "GitHub Actions has had 4 partial outages affecting our deployments. Self-hosted GitLab CI on EC2 with 16 runners gives us control over availability and secrets. Also enables private runner network access to internal services.",
        "Buildkite, CircleCI self-hosted, TeamCity, keep GitHub Actions with redundancy",
        "GitLab migration must not disrupt active sprints; teams migrate one pipeline at a time over 8 weeks",
        14,
    ),
    (
        "Replace Prometheus with Datadog for unified observability",
        "technical",
        "Ops team manages 3 separate observability tools (Prometheus, Grafana, Tempo). Datadog consolidates metrics, logs, and traces with better alerting UX and on-call integration. Ops team headcount freed up for platform work.",
        "Keep Prometheus + Grafana, Victoria Metrics, Elastic Observability",
        "Budget approved for Datadog at $12k/month; must cancel Grafana Cloud and reduce other observability spend",
        14,
    ),
    (
        "Implement multi-region active-active architecture",
        "architectural",
        "Single us-east-1 region means 45-minute RTO if AWS region has an outage. Active-active in us-west-2 provides < 5 minute RTO. DynamoDB Global Tables and Aurora Global Database replicate data with < 1 second lag.",
        "Active-passive with fast failover, us-east-1 only with better HA, multi-cloud active-active",
        "Active-active requires conflict resolution for concurrent writes — CRDT approach for order state",
        14,
    ),
    (
        "Adopt eBPF-based network observability with Cilium",
        "technical",
        "Kubernetes network policies are not observable — we cannot see which services are communicating. Cilium provides eBPF-based network observability with Hubble UI, enforces network policies, and replaces kube-proxy.",
        "Istio service mesh, Linkerd, Calico with Flow Logs, AWS VPC Flow Logs",
        "Cilium requires kernel 5.10+; verify all node AMIs meet requirement before migration",
        14,
    ),
    (
        "Implement zero-trust network access replacing VPN",
        "technical",
        "VPN access gives broad network access after authentication — violates least-privilege. Zero-trust with Cloudflare Access provides per-application identity-aware access with device posture checks. VPN decommissioned Q3 2026.",
        "BeyondCorp Enterprise, Tailscale, Zscaler, PAM solution only",
        "Cloudflare Access must support CLI tools (SSH, kubectl) used by engineers; browser rendering for web apps",
        15,
    ),
    (
        "Use LLM-based code review automation in CI",
        "technical",
        "Code review is bottleneck — average 3.2 days from PR open to merge. Claude API reviews PRs for security issues, missing tests, and code quality in < 2 minutes. Human reviewer focuses on architecture and domain logic.",
        "SonarQube, DeepSource, keep manual review only, CodeRabbit",
        "AI review is advisory only — cannot block merge; must not expose code to third-party APIs in air-gapped repos",
        15,
    ),
    (
        "Implement supply chain security with SLSA Level 2",
        "technical",
        "Log4Shell-type vulnerabilities require ability to trace any dependency back to its build provenance. SLSA Level 2 requires signed build artifacts and provenance attestation. GitHub Actions OIDC generates provenance; Sigstore Cosign signs images.",
        "SLSA Level 1 only, manual SBOM generation, Snyk Container only",
        "All container images must have provenance attestation before Q2 2026 enterprise customer audit",
        15,
    ),
    (
        "Adopt platform engineering with Internal Developer Platform",
        "architectural",
        "Each team reinvents deployment, monitoring, and secret management setup. IDP with Backstage provides self-service service creation, standardised golden paths, and tech radar. Reduces new service time-to-production from 3 weeks to 1 day.",
        "Expand platform team responsibilities, more documentation, developer portals without automation",
        "IDP must not become bottleneck; teams must retain ability to diverge from golden path with documented justification",
        15,
    ),
    (
        "Implement SBOM generation for all container images",
        "technical",
        "No visibility into transitive dependencies in production containers. Syft generates SBOM in SPDX format; Grype scans for CVEs against SBOM. Daily scan of production images alerts on new critical CVEs within 2 hours.",
        "Snyk Container, Aqua Security, manual Dockerfile analysis",
        "SBOM must be attached to container image as OCI artifact; scan results integrated into security dashboard",
        15,
    ),
    (
        "Migrate from REST to event-driven architecture for B2B integrations",
        "architectural",
        "Synchronous B2B API calls from suppliers fail during our maintenance windows causing partner escalations. AsyncAPI over Kafka gives suppliers fire-and-forget semantics with guaranteed delivery. Reduces integration support tickets 70%.",
        "GraphQL subscriptions, WebSockets, Webhooks only, keep REST with retry",
        "AsyncAPI spec must be published to developer portal; partners need 6-month migration window",
        16,
    ),
    (
        "Use DuckDB for ad-hoc analytics replacing direct Redshift queries",
        "technical",
        "Data analysts run heavy analytical queries directly on Redshift disrupting ETL jobs. DuckDB on analyst laptops queries S3 Parquet files directly with SQL, eliminating load on Redshift. Query performance comparable for exploratory work.",
        "Redshift Serverless, Athena, Snowflake, keep Redshift with workload management",
        "Parquet files must be updated with incremental ETL; analysts need data freshness SLA documented",
        16,
    ),
    (
        "Implement GitOps for ML model deployment",
        "technical",
        "ML models promoted to production via manual file copy with no version control. MLflow tracks model versions; ArgoCD deploys model server updates via GitOps. Rollback to previous model version in 3 minutes.",
        "SageMaker Model Registry, custom model server with blue-green, AWS Bedrock for all AI",
        "Model serving infrastructure separate from application GitOps repo; model card required for all production models",
        16,
    ),
    (
        "Adopt Anthropic Claude claude-sonnet-4-6 for all LLM workloads",
        "technical",
        "Current AI vendor mix (OpenAI, Claude) creates dual billing, API management overhead, and inconsistent behaviour across features. Consolidating on Claude claude-sonnet-4-6 reduces per-token cost 30% vs GPT-4o at same benchmark performance. Single SDK, single contract.",
        "Stay multi-vendor, migrate to GPT-4o, use AWS Bedrock for model abstraction, deploy open-source Llama",
        "OpenAI contract has 3-month notice period; migration must complete before renewal date",
        16,
    ),
    (
        "Implement database sharding for high-volume write tables",
        "technical",
        "Orders table approaching 500M rows causing write latency spikes during peak. Hash sharding by customer_id across 4 PostgreSQL instances. Application-layer shard routing with consistent hashing.",
        "Citus distributed PostgreSQL, CockroachDB, denormalise to DynamoDB, archive old data only",
        "Cross-shard queries must be avoided in hot paths; reporting queries fan-out to all shards",
        17,
    ),
    (
        "Build AI-powered demand forecasting replacing Excel models",
        "product",
        "Supply chain planning uses manually-maintained Excel models refreshed weekly. Time-series forecasting with Prophet + Claude-generated insights reduces forecast error 40% and updates hourly. Integrates with inventory management.",
        "ARIMA models, AWS Forecast, keep Excel with automation, buy demand planning software",
        "Model accuracy must be validated over 3 months before deprecating Excel fallback",
        17,
    ),
    (
        "Implement API monetisation with usage-based billing",
        "product",
        "CaaS-style API for paper industry partners is currently free. Usage-based billing via Stripe Billing with per-API-call metering enables new revenue stream. Freemium tier: 10k calls/month free, then $0.001/call.",
        "Subscription tiers only, one-time licence fee, keep free API, professional services model",
        "Billing system must handle retroactive credits; disputed charges need audit trail back to raw API logs",
        17,
    ),
    (
        "Use Valkey (Redis fork) replacing Redis for caching",
        "technical",
        "Redis BSL licence change in 7.4 means commercial redistribution now requires Redis licence. Valkey is the open-source fork maintained by AWS, Google, and Snap. Drop-in compatible with Redis 7.2 API. No migration needed beyond version bump.",
        "Keep Redis 7.2 (before BSL), switch to Memcached, Dragonfly, DragonflyDB",
        "Valkey 8.0 must pass compatibility tests against our Redis client library (redis-py 5.0) before promotion",
        17,
    ),
    (
        "Implement automated compliance reporting for SOC 2 Type II",
        "constraint",
        "Enterprise customers require SOC 2 Type II report annually costing $80k in audit fees. Automated evidence collection via AWS Security Hub, CloudTrail, and Config reduces audit prep time from 6 weeks to 3 days.",
        "Manual evidence collection with better tooling, Drata, Vanta, Tugboat Logic",
        "Evidence must be collected continuously (not point-in-time); 12-month observation period required for Type II",
        17,
    ),
    (
        "Replace Kubernetes Ingress with AWS ALB Ingress Controller",
        "technical",
        "Nginx Ingress requires manual SSL cert rotation and doesn't integrate with AWS WAF. ALB Ingress Controller provisions ALBs directly; ACM handles cert rotation automatically. WAF rules apply at ALB layer before traffic hits cluster.",
        "Traefik, Envoy Gateway, Istio Ingress, keep Nginx",
        "ALB cost increases by ~$200/month vs Nginx on EC2; justified by reduced operational overhead",
        18,
    ),
    (
        "Implement GraphQL Federation for public API gateway",
        "technical",
        "Each domain team maintains a separate GraphQL schema causing fragmented mobile developer experience. Apollo Federation v2 composes schemas from all subgraphs into a single public supergraph. Mobile app gets one endpoint for all data.",
        "REST API gateway, Schema stitching (deprecated), separate GraphQL per domain with client orchestration",
        "Subgraph ownership per domain team; breaking changes in subgraph require RFC and 30-day migration window",
        18,
    ),
    (
        "Adopt Green Software Foundation principles for carbon-aware computing",
        "constraint",
        "ESG report requires quantification and reduction of digital carbon footprint. AWS Carbon Footprint Tool + GreenOps dashboard tracks per-service carbon intensity. Batch jobs scheduled during low-carbon grid windows.",
        "Carbon offsets only, no measurement, manual optimisation",
        "Carbon metrics added to all service scorecards; teams with highest intensity get FinOps review",
        18,
    ),
    (
        "Implement rate limiting and DDoS protection with Cloudflare",
        "technical",
        "AWS WAF blocked 3 DDoS events but required 30-minute human response. Cloudflare Magic Transit provides autonomous DDoS mitigation < 3 seconds. Bot management blocks 98% of automated abuse without false positives on real users.",
        "AWS Shield Advanced, Akamai, keep WAF only, Fastly",
        "DNS delegation to Cloudflare required; certificate authority must change from ACM to Cloudflare-issued",
        18,
    ),
    (
        "Build self-healing infrastructure with AWS Systems Manager Automation",
        "technical",
        "On-call team manually remediates 80% of alerts that have known runbooks. SSM Automation documents codify runbooks; CloudWatch alarm actions trigger automated remediation. Reduces 3am pages 65% in pilot.",
        "Ansible playbooks triggered by alerts, PagerDuty runbook automation, manual only",
        "Automation must be validated in staging before production; all actions logged to CloudTrail",
        18,
    ),
    (
        "Implement developer productivity platform with AI coding assistants",
        "product",
        "Developer satisfaction survey shows IDE tooling as top pain point. Standardising on Claude Code + Cursor AI for all engineers with company-paid subscriptions. Custom context injection via smm-sync ensures AI has project-specific knowledge.",
        "GitHub Copilot Enterprise, Tabnine, CodeWhisperer, no AI assistants",
        "All AI-assisted code must be reviewed by human; sensitive codebases (crypto, auth) excluded from AI assistance",
        19,
    ),
    (
        "Migrate legacy document archive to S3 Intelligent-Tiering",
        "technical",
        "12TB of scanned paper documents in S3 Standard costing $276/month. Intelligent-Tiering automatically moves to cheaper tiers based on access patterns. Documents older than 90 days will be archived; expected cost reduction 73%.",
        "Glacier Deep Archive for all, manual lifecycle policy, keep Standard, EFS for easier access",
        "Retrieval SLA for archived documents must be < 24 hours; finance has emergency access requirement",
        19,
    ),
    (
        "Adopt AI-generated architecture decision records",
        "product",
        "Architecture decisions are made in Slack and never documented. Claude Code with smm-sync hook automatically generates ADRs from meeting transcripts and code review comments. Reduces documentation debt from 300 undocumented decisions.",
        "Manual ADR process with template, Notion AI, Confluence with AI plugin",
        "Generated ADRs require human approval before publishing; smm-sync provides the approval workflow",
        19,
    ),
    (
        "Implement network segmentation with AWS Transit Gateway",
        "technical",
        "All VPCs peered in hub-and-spoke model with 43 peering connections. Transit Gateway simplifies to central hub architecture, reduces BGP complexity, and enables centralised network monitoring. Scales to 1000 VPC attachments.",
        "Keep VPC peering, AWS PrivateLink for all internal traffic, Aviatrix",
        "Transit Gateway must be in centralised networking account; attach VPCs via RAM share",
        19,
    ),
    (
        "Build unified data platform with Apache Iceberg table format",
        "architectural",
        "Multiple data stores (Redshift, DynamoDB, S3 Parquet) have inconsistent schemas and no time-travel capability. Apache Iceberg provides ACID transactions on S3, schema evolution, and time-travel queries across all data. Unifies analytics and operational data patterns.",
        "Delta Lake, Apache Hudi, keep current stores, Apache XTable for interoperability",
        "Iceberg catalog (Glue) must support all compute engines used (Spark, Trino, DuckDB, Athena)",
        19,
    ),
    (
        "Adopt AI-assisted incident response with automated root cause analysis",
        "technical",
        "MTTR is 47 minutes due to manual log correlation across 15 services. Claude API analyses CloudWatch Logs, traces, and recent deploys to produce root cause hypothesis in < 2 minutes. On-call engineers validate hypothesis rather than searching blind.",
        "Grafana Incident, PagerDuty AIOps, Moogsoft, manual runbooks only",
        "AI hypothesis must show confidence score and supporting evidence; engineer must confirm before remediation",
        20,
    ),
    (
        "Implement predictive auto-scaling based on business metrics",
        "technical",
        "Reactive auto-scaling causes 5-7 minute latency spikes during anticipated load (Black Friday, monthly billing cycle). Predictive scaling uses historical patterns + business calendar to pre-provision capacity 30 minutes before events.",
        "Conservative over-provisioning, KEDA with custom metrics, keep reactive scaling",
        "Predictive model requires 6 months of historical data before enabling; fallback to reactive always active",
        20,
    ),
    (
        "Build API-first integrations for paper industry EDI modernisation",
        "product",
        "EDI X12 integrations with 40 paper distributors require specialised consultants costing $3k/integration. Claude-powered EDI-to-JSON translation reduces new partner onboarding from $3k to $200. Self-service portal for smaller distributors.",
        "AS2 gateway, SPS Commerce, TrueCommerce, keep EDI-only with better tooling",
        "EDI translation must achieve 99.9% accuracy on test corpus of 10k historical transactions before launch",
        20,
    ),
    (
        "Implement continuous compliance with AWS Config Rules",
        "constraint",
        "Quarterly compliance scans find violations that have been in place for months. Config Rules provide real-time drift detection against compliance benchmarks (CIS, PCI DSS, HIPAA). Auto-remediation for low-risk violations.",
        "Manual quarterly audits, Prisma Cloud, Wiz, Snyk Infrastructure as Code",
        "Auto-remediation rules must be approved by CISO before enabling; list maintained in security runbook",
        20,
    ),
    (
        "Use AI agents for automated supplier onboarding",
        "product",
        "Supplier onboarding takes 3 weeks and involves 12 manual steps across 4 systems. AI agent workflow uses Claude API to process supplier documentation, validate data, and provision API credentials. Reduces to 2 hours with human sign-off on final activation.",
        "Low-code RPA (UiPath), manual with better checklists, dedicated onboarding team",
        "Agent cannot access production systems autonomously; all write actions require human approval step",
        20,
    ),
]

# ── Contradiction pairs ───────────────────────────────────────────────────────
# (title_a, title_b, explanation, status, winner_is_a)
# title_a = newer decision, title_b = older decision

CONTRADICTION_DEFINITIONS = [
    (
        "Replace Datadog with OpenTelemetry + Grafana Cloud",
        "Add Datadog APM for performance monitoring",
        "Decision in Sprint 5 contradicts the Sprint 3 decision to adopt Datadog APM. The team replaced Datadog with OpenTelemetry after the Datadog bill exceeded budget. These decisions represent an architectural reversal on observability tooling.",
        "resolved",
        True,  # newer wins
        "jim.halpert",
    ),
    (
        "Replace Prometheus with Datadog for unified observability",
        "Replace Datadog with OpenTelemetry + Grafana Cloud",
        "Sprint 14 decision to re-adopt Datadog contradicts the Sprint 5 decision to replace Datadog with OpenTelemetry. The ops team preference overrides the cost optimisation rationale from Sprint 5. This is a circular dependency in observability strategy.",
        "resolved",
        True,
        "michael.scott",
    ),
    (
        "Migrate from Kubernetes to AWS ECS Fargate for stateless services",
        "Deploy Kubernetes (EKS) for container orchestration",
        "Sprint 13 migration away from Kubernetes partially contradicts the Sprint 4 decision to adopt EKS. ECS Fargate covers stateless services while EKS remains for stateful workloads — but the architectural direction has changed for the majority of services.",
        "resolved",
        True,
        "michael.scott",
    ),
    (
        "Replace GraphQL with gRPC for internal service communication",
        "Adopt GraphQL for mobile application API",
        "Sprint 13 adoption of gRPC for internal services conflicts with Sprint 4 GraphQL adoption. Resolution: GraphQL is retained for external/mobile APIs, gRPC for internal service mesh. The original decision did not scope external vs internal separately.",
        "resolved",
        False,  # both survive in different scopes
        "jim.halpert",
    ),
    (
        "Migrate from GitHub Actions to GitLab CI (self-hosted)",
        "Migrate CI/CD from Jenkins to GitHub Actions",
        "Sprint 14 migration to self-hosted GitLab CI contradicts the Sprint 11 migration away from Jenkins to GitHub Actions. The team migrated to GitHub Actions and then decided to migrate again 3 sprints later citing availability concerns.",
        "resolved",
        True,
        "michael.scott",
    ),
    (
        "Adopt Pulumi for infrastructure-as-code replacing Terraform",
        "Adopt Terraform for infrastructure-as-code replacing CloudFormation",
        "Sprint 14 Pulumi adoption contradicts Sprint 5 Terraform adoption which itself replaced Sprint 3 CloudFormation. This is the third IaC tool decision in 11 sprints, creating migration overhead and knowledge fragmentation across the team.",
        "resolved",
        True,
        "dwight.schrute",
    ),
    (
        "Replace LaunchDarkly with in-house feature flag service",
        "Implement feature flags with LaunchDarkly",
        "Sprint 13 decision to build in-house feature flags contradicts Sprint 7 decision to use LaunchDarkly. The cost argument ($36k vs $200/year) is compelling but introduces build-vs-buy risk and maintenance burden.",
        "resolved",
        True,
        "pam.beesly",
    ),
    (
        "Split monorepo into domain-aligned multi-repo structure",
        "Standardise on Python 3.12 for all new backend services",
        "Multi-repo structure in Sprint 13 makes enforcing a standard Python version harder — each repo now manages its own Python version independently. The constraint from Sprint 2 relied on monorepo tooling to enforce consistency.",
        "dismissed",
        None,
        "jim.halpert",
    ),
    (
        "Migrate from Celery to AWS Lambda for document processing",
        "Use Celery with SQS for async task processing",
        "Sprint 4 Lambda migration contradicts the Sprint 2 Celery adoption for async task processing. Lambda has cold start limitations that Celery does not. The decision was made for cost reasons but the architectural tradeoffs were not fully re-evaluated.",
        "resolved",
        True,
        "michael.scott",
    ),
    (
        "Migrate order service from Celery to Kafka consumer groups",
        "Migrate from Celery to AWS Lambda for document processing",
        "Sprint 7 Kafka consumer group decision partially reverses the Sprint 4 Lambda migration for order processing specifically. Lambda was kept for document processing but not for order processing, creating two async patterns for related workloads.",
        "dismissed",
        None,
        "dwight.schrute",
    ),
    (
        "Store sensitive documents in MinIO on-premises cluster",
        "Store all file assets in S3 with CloudFront CDN",
        "Sprint 9 MinIO on-prem requirement contradicts the Sprint 2 decision to store all file assets in S3. Regulatory constraints require a hybrid storage approach — compliance-sensitive documents on-prem, everything else in S3.",
        "resolved",
        True,
        "oscar.martinez",
    ),
    (
        "Implement hybrid cloud strategy for compliance-sensitive workloads",
        "Migrate on-premises data center to AWS (us-east-1 primary)",
        "Sprint 9 hybrid cloud strategy directly contradicts the Sprint 1 full cloud migration decision. Compliance requirements discovered post-Sprint 1 (EU AI Act, data sovereignty) require retaining on-prem infrastructure for specific workload categories.",
        "resolved",
        True,
        "oscar.martinez",
    ),
    (
        "Use Kafka for real-time event streaming between domains",
        "Use AWS EventBridge for cross-service event routing",
        "Sprint 6 Kafka adoption for real-time streaming creates overlap with Sprint 5 EventBridge adoption. Both are event routing systems. The team should define clear boundaries: EventBridge for low-volume cross-account routing, Kafka for high-throughput domain events.",
        "resolved",
        True,
        "jim.halpert",
    ),
    (
        "Adopt EU AI Act Article 12 audit logging for AI systems",
        "Implement GDPR data subject request automation",
        "Article 12 requires immutable audit logs for AI systems while GDPR right-to-erasure requires the ability to delete personal data from all systems including audit logs. These two constraints are in direct conflict for AI systems that process personal data.",
        "pending",
        None,
        None,
    ),
    (
        "Decompose monolithic Java EAR application into microservices",
        "Standardise on Python 3.12 for all new backend services",
        "Microservices decomposition creates multiple language runtimes if teams choose their own stack. The Python 3.12 standardisation conflicts with domain teams wanting to keep Java for services being carved out of the Java monolith.",
        "resolved",
        False,  # Python wins for new services, Java for carved-out services
        "michael.scott",
    ),
    (
        "Implement database sharding for high-volume write tables",
        "Use AWS Aurora Serverless v2 for variable-load databases",
        "Application-layer sharding contradicts the Sprint 6 Aurora Serverless v2 decision which relies on Aurora's managed scaling. Custom sharding requires moving away from Aurora to vanilla PostgreSQL instances, losing the serverless scaling benefit.",
        "pending",
        None,
        None,
    ),
    (
        "Replace GraphQL with gRPC for internal service communication",
        "Implement API gateway pattern with Kong",
        "Kong API gateway was deployed in Sprint 5 with GraphQL support as a key use case. Replacing internal GraphQL with gRPC requires Kong gRPC-JSON transcoding plugins and changes the gateway configuration significantly.",
        "resolved",
        True,
        "jim.halpert",
    ),
    (
        "Implement GraphQL Federation for public API gateway",
        "Replace GraphQL with gRPC for internal service communication",
        "GraphQL Federation for public APIs coexists with internal gRPC, but the Apollo Federation supergraph composition requires each domain to maintain both a gRPC service and a GraphQL subgraph wrapper, doubling the API surface.",
        "pending",
        None,
        None,
    ),
    (
        "Adopt Anthropic Claude claude-sonnet-4-6 for all LLM workloads",
        "Integrate Claude API for automated document processing",
        "The Sprint 16 vendor consolidation decision retroactively endorses the Sprint 9 Claude adoption. However, the Sprint 9 decision evaluated Claude for document processing specifically, not as a general-purpose LLM replacement. The scope expansion may miss use-case-specific evaluation.",
        "dismissed",
        None,
        "pam.beesly",
    ),
    (
        "Implement zero-trust network access replacing VPN",
        "Enforce TLS 1.3 for all internal and external traffic",
        "Zero-trust network access proxies all traffic through Cloudflare Access, which terminates TLS and re-encrypts. This means some internal traffic traverses Cloudflare's infrastructure breaking the guarantee that TLS 1.3 is end-to-end. Need architectural review.",
        "pending",
        None,
        None,
    ),
    (
        "Use DuckDB for ad-hoc analytics replacing direct Redshift queries",
        "Implement data mesh architecture for analytics",
        "Data mesh with dbt on Redshift per domain contradicts DuckDB on S3 Parquet approach. Data mesh requires data products in Redshift; DuckDB approach bypasses Redshift entirely for analytics. Two competing analytical patterns create consistency risk.",
        "pending",
        None,
        None,
    ),
    (
        "Implement rate limiting and DDoS protection with Cloudflare",
        "Use AWS CloudFront with WAF for edge security",
        "Sprint 18 Cloudflare adoption as primary edge layer duplicates Sprint 3 CloudFront + WAF. Both provide DDoS protection, WAF, and CDN. Running both adds cost and complexity. Need to decide primary edge layer and deprecate the other.",
        "pending",
        None,
        None,
    ),
    (
        "Build unified data platform with Apache Iceberg table format",
        "Implement data mesh architecture for analytics",
        "Apache Iceberg in Sprint 19 provides table-level ACID and schema evolution but the Sprint 11 data mesh uses Redshift as each domain's data store. Migrating domain data products to Iceberg tables changes the data mesh contract and requires renegotiating data product SLAs.",
        "pending",
        None,
        None,
    ),
    (
        "Implement predictive auto-scaling based on business metrics",
        "Migrate from Kubernetes to AWS ECS Fargate for stateless services",
        "Predictive scaling was designed for Kubernetes KEDA but ECS Fargate uses different scaling primitives. The implementation approach from Sprint 20 assumes Kubernetes HPA and needs redesign for ECS Application Auto Scaling target tracking.",
        "resolved",
        True,
        "michael.scott",
    ),
    (
        "Centralise secret management in AWS Secrets Manager",
        "Use SOPS + age encryption for secrets in GitOps repo",
        "Secrets Manager centralises secret storage and rotation. SOPS/age encrypts secrets for GitOps storage but the secrets still need to come from somewhere. Running both creates two sources of truth for secrets — one in Secrets Manager, one in Git encrypted.",
        "resolved",
        True,
        "dwight.schrute",
    ),
    (
        "Migrate from REST to event-driven architecture for B2B integrations",
        "Replace SOAP supplier API with RESTful JSON API",
        "Sprint 16 event-driven B2B contradicts Sprint 1 REST API adoption for suppliers. The team migrated from SOAP to REST in Sprint 1 and is now moving to async event-driven 15 sprints later. Suppliers who just completed the REST migration face another migration.",
        "resolved",
        True,
        "pam.beesly",
    ),
    (
        "Deploy PostgreSQL on AWS RDS Multi-AZ with read replicas",
        "Use AWS Aurora Serverless v2 for variable-load databases",
        "RDS Multi-AZ with read replicas (Sprint 2) and Aurora Serverless v2 (Sprint 6) both manage PostgreSQL but are different AWS services. The team has both deployed for different use cases creating operational split-brain on which PostgreSQL service to use for new workloads.",
        "resolved",
        True,
        "michael.scott",
    ),
    (
        "Implement supply chain security with SLSA Level 2",
        "Migrate CI/CD from Jenkins to GitHub Actions",
        "GitHub Actions OIDC provides the provenance attestation required for SLSA Level 2. After the Sprint 14 decision to migrate to GitLab CI, SLSA Level 2 implementation must be redesigned for GitLab CI's different OIDC token format and Sigstore integration.",
        "pending",
        None,
        None,
    ),
    (
        "Adopt GitOps with ArgoCD for Kubernetes deployments",
        "Migrate from Kubernetes to AWS ECS Fargate for stateless services",
        "ArgoCD GitOps operates on Kubernetes resources. Sprint 13 ECS Fargate migration removes Kubernetes for stateless services, making ArgoCD the deployment tool for only stateful services. Need to evaluate AWS App Mesh or CDK Pipelines for Fargate deployment automation.",
        "resolved",
        True,
        "jim.halpert",
    ),
    (
        "Adopt Pulumi for infrastructure-as-code replacing Terraform",
        "Adopt Infrastructure Cost Allocation Tags on all AWS resources",
        "Terraform modules enforced cost allocation tags via Terraform Sentinel policies. Pulumi migration requires reimplementing tag enforcement in Pulumi policy-as-code (CrossGuard). During migration window, tag enforcement is effectively unenforced.",
        "resolved",
        True,
        "dwight.schrute",
    ),
    # Extra to hit ~50
    (
        "Add ElasticSearch for full-text search across orders and documents",
        "Implement DynamoDB for product catalogue search index",
        "Two separate search systems — ElasticSearch for orders/documents (Sprint 7) and DynamoDB for product catalogue (Sprint 4) — create operational complexity. Engineers must understand two query paradigms for what is fundamentally the same use case.",
        "dismissed",
        None,
        "jim.halpert",
    ),
    (
        "Use Kafka for real-time event streaming between domains",
        "Use AWS EventBridge for cross-service event routing",
        "Kafka and EventBridge overlap for internal event routing but serve different patterns. EventBridge handles low-volume cross-account and external events; Kafka handles high-throughput domain events. Boundary needs clearer documentation to prevent ad-hoc tool selection.",
        "dismissed",
        None,
        "michael.scott",
    ),
    (
        "Implement webhook delivery system for partner integrations",
        "Migrate from REST to event-driven architecture for B2B integrations",
        "Webhooks are a subset of event-driven integration patterns. Sprint 7 webhook system and Sprint 16 AsyncAPI over Kafka are both being built for B2B partner notifications, creating potential duplication. Webhooks may become redundant once Kafka-based AsyncAPI is fully deployed.",
        "resolved",
        True,
        "pam.beesly",
    ),
    (
        "Implement CQRS for order and inventory domains",
        "Adopt event sourcing for order state management",
        "Event sourcing defines the write side; CQRS defines the separation of command and query. These are complementary, not contradictory. However, the teams implementing them separately created slightly incompatible event schema conventions that need reconciliation.",
        "resolved",
        True,
        "jim.halpert",
    ),
    (
        "Implement row-level security in PostgreSQL for multi-tenant data",
        "Implement database sharding for high-volume write tables",
        "RLS policies are applied per-connection and rely on session variables for tenant context. Database sharding distributes data across multiple PostgreSQL instances — RLS policies must be replicated to all shards and the session variable mechanism works differently per-shard.",
        "pending",
        None,
        None,
    ),
    (
        "Migrate from Jenkins to GitHub Actions",
        "Migrate from GitHub Actions to GitLab CI (self-hosted)",
        "Third CI/CD migration in 4 sprints. Jenkins → GitHub Actions (Sprint 11) → GitLab CI (Sprint 14). Each migration takes 6-8 weeks. The team is spending more time migrating CI/CD tooling than delivering features. Pattern of tooling churn needs architectural review.",
        "resolved",
        True,
        "michael.scott",
    ),
    (
        "Replace Datadog with OpenTelemetry + Grafana Cloud",
        "Replace Prometheus with Datadog for unified observability",
        "The observability tooling has oscillated: Datadog (S3) → OpenTelemetry/Grafana (S5) → Datadog (S14). The second Datadog adoption comes with a different budget justification but ignores the operational knowledge built around the OpenTelemetry stack.",
        "resolved",
        True,
        "michael.scott",
    ),
    (
        "Implement circuit breaker pattern with Resilience4j",
        "Implement API gateway pattern with Kong",
        "Circuit breakers in Resilience4j at the service level and Kong gateway-level circuit breakers both exist. Running circuit breakers at two layers creates confusion about which layer is responsible for fallback behaviour when a circuit opens.",
        "resolved",
        False,
        "jim.halpert",
    ),
    (
        "Use AWS Cognito for customer identity management",
        "Upgrade to OAuth 2.0 + SAML 2.0 for enterprise SSO",
        "OAuth 2.0/SAML for enterprise SSO and Cognito for customer identity can coexist but the team initially planned to use Cognito for both. The Sprint 4 enterprise SSO decision was made before Sprint 8 Cognito — requiring Cognito to be configured as both a SP for enterprise SAML and an IdP for customers.",
        "resolved",
        True,
        "dwight.schrute",
    ),
    (
        "Implement network segmentation with AWS Transit Gateway",
        "Implement zero-trust network access replacing VPN",
        "Transit Gateway manages network-level routing between VPCs while zero-trust access controls application-level access. These are complementary but the zero-trust implementation was scoped to replace VPN for human access, not service-to-service. Boundary between the two needs documentation.",
        "dismissed",
        None,
        "dwight.schrute",
    ),
    (
        "Adopt eBPF-based network observability with Cilium",
        "Adopt GitOps with ArgoCD for Kubernetes deployments",
        "Cilium CNI installation and configuration is managed outside of ArgoCD GitOps because CNI plugins must be installed before the cluster is operational. This creates a bootstrapping exception to the GitOps principle where Cilium config is applied manually.",
        "resolved",
        True,
        "jim.halpert",
    ),
    (
        "Implement AI model versioning and rollback",
        "Adopt Anthropic Claude claude-sonnet-4-6 for all LLM workloads",
        "Model versioning pins to specific API model versions. Vendor consolidation to Claude claude-sonnet-4-6 means all model version pins need to be updated. The versioning strategy must account for Claude API versioning semantics which differ from OpenAI's versioning approach.",
        "resolved",
        True,
        "pam.beesly",
    ),
    (
        "Implement SBOM generation for all container images",
        "Migrate from Kubernetes to AWS ECS Fargate for stateless services",
        "SBOM generation in CI produces OCI-attached attestations for container images. ECS Fargate task definitions reference container images but the AWS ECS console does not natively display OCI attestations. The SBOM verification workflow must be updated for ECS.",
        "resolved",
        True,
        "dwight.schrute",
    ),
    (
        "Adopt platform engineering with Internal Developer Platform",
        "Split monorepo into domain-aligned multi-repo structure",
        "IDP service templates assume a monorepo for scaffolding new services. Multi-repo structure requires the IDP to create repositories, set up CI, and configure permissions across multiple GitHub organisations. IDP complexity increases significantly.",
        "pending",
        None,
        None,
    ),
    (
        "Use AI agents for automated supplier onboarding",
        "Build API-first integrations for paper industry EDI modernisation",
        "AI agent workflow and EDI modernisation API both handle supplier onboarding but from different angles. The AI agent automates the human-facing onboarding steps while EDI modernisation handles technical integration. Unclear handoff boundary between the two systems.",
        "pending",
        None,
        None,
    ),
]


# ── Synthetic decision templates (fills corpus to ~400) ──────────────────────
# (title_template, type, rationale_template, alternatives, constraints, sprint_range)

SYNTHETIC_TEMPLATES = [
    # Security hardening decisions
    ("Enable AWS GuardDuty for threat detection across all accounts", "technical",
     "Security team identified 12 undetected intrusion attempts in CloudTrail logs last quarter. GuardDuty ML-based threat detection covers VPC flow, DNS, and CloudTrail anomalies without agent installation. Mean detection time drops from days to minutes.",
     "Splunk SIEM, Sumo Logic, manual CloudTrail analysis", "GuardDuty findings must route to PagerDuty via EventBridge; suppress known-good patterns in first 30 days", (2, 4)),
    ("Implement mandatory MFA for all AWS IAM users", "constraint",
     "IAM credential compromise is the #1 cause of AWS breaches per AWS security report. Mandatory MFA eliminates 99.9% of account compromise attacks from stolen credentials. Virtual MFA with TOTP enforced via SCP across all accounts.",
     "Hardware tokens, IAM Identity Center only, IP allowlisting", "Break-glass accounts exempt but locked in physical safe with dual-custody procedure", (1, 3)),
    ("Rotate all IAM access keys on 90-day cycle", "constraint",
     "Long-lived IAM keys create persistent risk if compromised. Automated rotation via Lambda detects keys > 90 days and notifies owners. Keys > 120 days are automatically disabled after 48-hour warning.",
     "Eliminate IAM keys entirely (OIDC only), manual rotation reminders, 180-day cycle", "Service accounts using IAM keys must migrate to instance roles within 6 months", (3, 5)),
    ("Deploy AWS Security Hub for centralised security findings", "technical",
     "Security findings spread across GuardDuty, Inspector, Macie, and Config with no unified view. Security Hub aggregates findings and applies CIS AWS Foundations Benchmark scoring. Executive security dashboard shows compliance posture in real-time.",
     "Splunk, Elastic SIEM, manual aggregation in spreadsheet", "All findings must have SLA for remediation: Critical 24h, High 72h, Medium 30 days", (4, 6)),
    ("Implement AWS Inspector for continuous EC2 and container vulnerability scanning", "technical",
     "Last vulnerability scan was 6 months ago via manual Nessus. Inspector 2.0 continuously scans EC2, Lambda, and container images, correlating CVEs with network reachability. Critical CVEs on internet-facing resources alert within 5 minutes.",
     "Qualys, Tenable Nessus, Snyk Container, manual patching schedule", "Inspector findings for critical CVEs block AMI promotions in CI/CD pipeline", (5, 8)),
    # Performance & scalability
    ("Implement HTTP/2 push for critical CSS and JS assets", "technical",
     "First-contentful-paint on product pages averages 2.8s due to sequential resource loading. HTTP/2 push from ALB with preload headers delivers critical assets before browser parses HTML. Core Web Vitals LCP improves from 2.8s to 1.4s in staging tests.",
     "HTTP/3 QUIC, CDN preload, inline critical CSS, service workers", "HTTP/2 push must be disabled for non-critical assets to avoid cache pollution", (5, 7)),
    ("Add PostgreSQL query plan caching with prepared statements", "technical",
     "PostgreSQL spending 15% of CPU on query planning for repeated identical queries. Prepared statements cache query plans after first execution. Benchmarks show 23% throughput improvement for high-frequency OLTP queries.",
     "pgBouncer statement pooling, application-level caching, ORM query cache", "Prepared statement cache invalidated on schema changes — deployment sequence must account for this", (3, 5)),
    ("Implement lazy loading for product catalogue images", "technical",
     "Catalogue pages load 80 images on initial render consuming 12MB of bandwidth per page view. Intersection Observer API lazy loads images as user scrolls. 67% reduction in initial page payload; LCP improves as hero images load first.",
     "Virtual scrolling, pagination only, server-side rendering with no client images", "IE11 requires polyfill for Intersection Observer — bundle size impact must be < 5KB", (6, 8)),
    ("Adopt CDN-level edge caching for API responses", "technical",
     "GraphQL queries for product data hit origin on every request despite data changing hourly. CloudFront caches GET/HEAD responses with Cache-Control headers. API cache hit rate target 85% for public catalogue endpoints.",
     "Varnish, nginx proxy_cache, Redis HTTP cache, no caching", "Authenticated endpoints must never be cached; Vary headers required for content negotiation", (7, 9)),
    ("Implement async pre-warming for Lambda cold starts", "technical",
     "Lambda document processing functions have 2.3s cold start affecting user-facing operations. Scheduled EventBridge rule pings functions every 5 minutes to keep instances warm. P99 cold start drops from 2.3s to 180ms.",
     "Provisioned concurrency (expensive), migrate back to ECS, keep cold starts and accept latency", "Pre-warming adds $40/month per function — only apply to user-facing functions, not batch jobs", (5, 7)),
    # Data & analytics
    ("Implement Change Data Capture with Debezium", "technical",
     "ETL jobs run nightly causing analytics data to be 24h stale. Debezium captures PostgreSQL WAL changes and streams to Kafka in real-time. Analytics database latency drops from 24h to < 30 seconds for most tables.",
     "AWS DMS, custom triggers, logical replication without Debezium, batch ETL improvement", "Debezium requires PostgreSQL logical replication enabled — verify RDS parameter group before enabling", (6, 8)),
    ("Adopt dbt (data build tool) for analytics transformations", "technical",
     "SQL transformations for analytics scattered across 23 stored procedures with no documentation. dbt models are version-controlled SQL with auto-generated documentation and lineage graphs. CI runs dbt test on every PR.",
     "Spark SQL, custom Python ETL, Airflow with raw SQL, Matillion", "dbt models must have at least one schema test per table before production promotion", (8, 10)),
    ("Deploy Apache Airflow on MWAA for ETL orchestration", "technical",
     "Cron jobs running ETL on EC2 have no retry, no monitoring, and no dependency management. Apache Airflow on MWAA provides DAG-based orchestration with retry policies, alerting, and visual task graph. Migrating 34 cron jobs to DAGs.",
     "AWS Glue workflows, Prefect, Dagster, Luigi, keep cron jobs", "All DAGs must have SLA miss callbacks; critical DAGs must alert on first failure not after 3 retries", (9, 11)),
    ("Implement data quality checks with Great Expectations", "technical",
     "Downstream analytics failures discovered 3 bad data incidents last quarter that corrupted monthly reports. Great Expectations defines data contracts as executable tests run in CI and on every pipeline execution. Failures halt downstream processing.",
     "dbt tests only, custom Python assertions, manual spot checks, Monte Carlo", "Data quality failures must page on-call if they affect SLA-governed reporting datasets", (10, 12)),
    ("Use Redshift Spectrum for querying S3 data without loading", "technical",
     "Loading S3 event logs into Redshift takes 6 hours and consumes 40% of cluster resources during ingestion. Redshift Spectrum queries S3 Parquet directly using external tables. No ETL needed for exploratory queries on raw event data.",
     "Athena, Presto/Trino, EMR, move all data to Redshift", "External tables must use columnar Parquet format; CSV on S3 must be converted before Spectrum queries", (11, 13)),
    # Developer experience
    ("Standardise on pre-commit hooks for code quality", "technical",
     "Code style violations and import sorting inconsistencies consume 30 minutes of review per PR. pre-commit hooks run black, isort, flake8, and mypy locally before commit. CI runs identical checks to catch any bypassed hooks.",
     "Editor plugins only, CI-only checks, GitHub Actions auto-fix PR", "pre-commit config stored in repo root; engineers must run pre-commit install after clone", (2, 4)),
    ("Implement PR size limits and split guidelines", "technical",
     "Average PR has 847 lines of changes making thorough review impossible. PRs > 500 lines get automated comment requesting split into smaller units. Reduces review time per PR from 95 minutes to 23 minutes.",
     "Stacked PRs with graphite, mandatory pair programming, AI review assistance only", "Exceptions allowed for generated code (migrations, proto files) with explicit label", (3, 5)),
    ("Use conventional commits and semantic-release", "technical",
     "Changelog generation is manual and often skipped causing no release notes for 40% of releases. Conventional commits (feat, fix, chore) enable automated CHANGELOG.md generation and semantic version bumping. Release notes auto-posted to Slack.",
     "Keep free-form commits, manual changelog, GitHub releases only", "All repos must enforce conventional commit format via commitlint in CI before enabling semantic-release", (4, 6)),
    ("Implement automated dependency updates with Renovate", "technical",
     "Dependency updates are done quarterly by hand causing 200+ outdated packages at any time. Renovate auto-creates PRs for dependency updates grouped by type. Security updates get priority queue and auto-merge if CI passes.",
     "Dependabot, manual quarterly updates, Snyk auto-fix PRs", "Renovate config must group patch updates to avoid PR flood; major updates require human approval", (5, 7)),
    ("Create internal developer documentation portal with Backstage", "product",
     "API documentation spread across Confluence, Notion, Swagger UI, and READMEs with no central index. Backstage TechDocs compiles Markdown from repos into searchable portal. Service catalog tracks ownership, SLOs, and runbooks per service.",
     "Confluence overhaul, Gitbook, Notion workspace, GitHub wiki", "Each service must have a catalog-info.yaml before appearing in Backstage; migration over 3 sprints", (12, 14)),
    # Compliance & governance
    ("Implement AWS CloudTrail Lake for long-term audit log retention", "constraint",
     "CloudTrail logs in S3 are not queryable and 90-day retention is insufficient for SOC 2 Type II requirements. CloudTrail Lake provides SQL-queryable event history with 7-year retention. Audit queries that took 3 hours now complete in 30 seconds.",
     "S3 + Athena for CloudTrail, SIEM ingestion, manual log retrieval", "CloudTrail Lake must be enabled in all regions; data event logging adds 40% to CloudTrail cost — selective enabling required", (10, 12)),
    ("Enforce tag policies via AWS Organizations SCPs", "constraint",
     "Untagged resources reached 34% of total AWS spend making cost attribution impossible. Service Control Policies reject resource creation without mandatory cost-centre, team, and environment tags. Applied to all non-root accounts.",
     "Tag enforcement via Config rules (reactive), Terraform policy-as-code only, manual audits", "SCP must have exception process for emergency provisioning; 48-hour grace period for automated provisioning tools", (7, 9)),
    ("Implement DAST scanning with OWASP ZAP in CI", "technical",
     "Static analysis catches code-level vulnerabilities but not runtime misconfigurations. OWASP ZAP in active scan mode against staging environment catches injection, XSS, and misconfigurations before production. Integrated into deployment pipeline.",
     "Burp Suite Enterprise, Veracode DAST, manual penetration testing only", "DAST scans must not run against production; test data must be sanitised in staging environment", (8, 10)),
    ("Deploy AWS Config for continuous resource compliance monitoring", "technical",
     "Compliance violations discovered only during quarterly audits are costly to remediate. AWS Config records all resource configuration changes and evaluates against compliance rules continuously. Drift alerts fire within 15 minutes of violation.",
     "Terraform plan in CI only, manual monthly audits, Cloud Custodian", "Config rules must map to specific compliance requirements (SOC 2, PCI) in documentation", (6, 8)),
    ("Implement data retention policies with automated S3 lifecycle rules", "constraint",
     "Legal requires 7-year retention for financial documents and 3-year for operational data. S3 lifecycle rules automatically transition to Glacier after 90 days and delete after retention period. Reduces S3 spend 55% through automated tiering.",
     "Manual deletion scripts, Glacier Vault Lock, keep all data in S3 Standard indefinitely", "Retention policy must be reviewed by Legal annually; delete operations must be logged to immutable audit log", (9, 11)),
    ("Establish API deprecation policy with sunset headers", "technical",
     "3 API breaking changes last year caused unplanned partner outages. Sunset HTTP header (RFC 8594) notifies clients of deprecation dates. 90-day minimum notice for non-breaking changes, 180 days for breaking. Tracked in API changelog.",
     "Email announcements only, version headers, no formal policy", "Policy must be documented in developer portal and API Gateway returns Sunset header from day-1 of deprecation", (5, 7)),
    # Cost optimisation
    ("Implement Spot Instance strategy for batch workloads", "technical",
     "Batch processing jobs run on On-Demand instances at full price despite being interruptible. Spot Instances reduce compute costs 70% for fault-tolerant batch workloads. Spot interruption handlers checkpoint job state to S3 for restart.",
     "Reserved Instances for batch, Graviton On-Demand, Fargate Spot", "Spot strategy must define maximum Spot price per instance type; fall back to On-Demand if no capacity", (7, 9)),
    ("Right-size EC2 instances based on Compute Optimizer recommendations", "technical",
     "15 EC2 instances running at < 10% average CPU utilisation identified by Compute Optimizer. Rightsizing to smaller instances saves $3,200/month with no performance impact. Graviton3 instances provide 20% better price-performance.",
     "Reserved Instance commitment changes, manual analysis, keep current sizes for safety buffer", "Rightsizing must be tested in staging for 2 weeks before production; memory-optimised workloads excluded", (8, 10)),
    ("Implement S3 Intelligent-Tiering for all application data buckets", "technical",
     "Application data buckets have 60% of objects not accessed in > 30 days but stored in S3 Standard. Intelligent-Tiering moves infrequently accessed objects to cheaper tiers automatically. No retrieval fees for Intelligent-Tiering unlike Glacier.",
     "Manual lifecycle policies, S3 Glacier Deep Archive for old data, keep Standard", "Intelligent-Tiering has per-object monitoring fee for objects > 128KB — analyse cost before enabling on high-object-count buckets", (9, 11)),
    ("Consolidate dev/test environments with auto-shutdown schedules", "technical",
     "Development and testing environments run 24/7 spending $8k/month during off-hours when no one is working. Lambda function shuts down non-production resources at 8pm and restarts at 7am on weekdays. Weekend shutdown saves additional 28%.",
     "Smaller always-on environments, manual start/stop, branch environments only", "Auto-shutdown must have 30-minute warning notification; override mechanism for on-call and late-night deployments", (6, 8)),
    ("Use AWS Graviton3 processors for 25% better price-performance", "technical",
     "All EC2 instances use x86 Intel processors. Graviton3 ARM-based instances provide 25% better performance per dollar. Python, Java, and Node applications run without code changes on ARM.",
     "AMD EPYC instances, keep Intel, custom hardware procurement", "Native Python C extensions must be tested for ARM compatibility; migration per-service over 4 sprints", (10, 12)),
    # Integration & messaging
    ("Implement idempotency keys for all payment operations", "technical",
     "Duplicate payment processing occurred twice last year when network retries hit our payment API. Idempotency keys cached in Redis for 24 hours prevent duplicate charges on retry. Stripe, Adyen, and internal payment APIs all support idempotency keys.",
     "Deduplication in payment processor only, database unique constraints, optimistic locking", "Idempotency key TTL must exceed maximum client retry window; key format must include transaction type to prevent cross-operation collisions", (4, 6)),
    ("Use AWS SQS Dead Letter Queues for all message processors", "technical",
     "Failed message processing silently drops messages with no visibility. DLQs capture messages that fail after 3 retries. CloudWatch alarm on DLQ depth triggers on-call. Failed messages are inspectable and replayable without code changes.",
     "Retry forever with backoff, manual error handling, separate error log table", "DLQ messages must have 14-day retention minimum; DLQ depth > 10 must alert within 5 minutes", (3, 5)),
    ("Implement outbox pattern for reliable event publishing", "technical",
     "Services publish events to Kafka after committing database transactions. If the service crashes after the DB commit but before Kafka publish, the event is lost. Outbox pattern writes events to DB in same transaction, then a relay process publishes to Kafka.",
     "Saga choreography only, two-phase commit, accept rare event loss", "Outbox relay must use at-least-once delivery; consumers must be idempotent by event ID", (7, 9)),
    ("Adopt AsyncAPI 2.6 spec for event-driven API documentation", "technical",
     "Kafka topic schemas and event contracts are undocumented causing consumer implementation errors. AsyncAPI spec documents all Kafka topics with message schemas, descriptions, and ownership. Auto-generated from schema registry.",
     "Confluent Schema Registry docs only, Avro IDL, manual documentation", "AsyncAPI spec changes must go through PR review same as OpenAPI; breaking schema changes require new topic version", (9, 11)),
    ("Implement request coalescing for high-frequency identical queries", "technical",
     "Product catalogue API receives 50k identical requests/second during peak for the same top-100 products. Request coalescing holds duplicate in-flight requests and resolves all with single backend query result. Reduces origin load 80% during traffic spikes.",
     "Aggressive caching only, rate limiting, separate cached endpoint", "Coalescing must not apply to personalised queries; max coalesce window 100ms to preserve freshness", (8, 10)),
    # Platform & infrastructure
    ("Implement GitOps-driven environment promotion", "technical",
     "Promoting code from staging to production requires 6 manual approvals in Jira, taking 2 days average. GitOps environment promotion merges staging branch to production with automated checks. Approval gate is a single PR review from team lead.",
     "Jenkins promotion pipeline, manual deploy scripts, ChatOps deploy commands", "Promotion must be blocked if any SLO violation active in staging; error budget check required", (8, 10)),
    ("Deploy Velero for Kubernetes backup and disaster recovery", "technical",
     "No Kubernetes cluster backup exists — namespace deletion is unrecoverable. Velero backs up all namespace resources and PVC snapshots to S3 every 6 hours. Recovery tested monthly; full cluster restore validated in DR drill.",
     "Manual etcd snapshots, AWS Backup for PVCs only, EKS managed node group AMI recovery", "Backup verification must include restore test to isolated cluster quarterly; RTO must be < 2 hours", (6, 8)),
    ("Implement cluster autoscaling with Karpenter", "technical",
     "EKS Cluster Autoscaler has 3-5 minute scale-out latency causing pending pods during traffic spikes. Karpenter provisions new nodes in < 60 seconds by directly calling EC2 API. Bin-packing optimisation reduces node count 30%.",
     "Keep Cluster Autoscaler with lower thresholds, pre-provisioned node pools, KEDA scale-from-zero", "Karpenter provisioner must respect node topology spread for AZ distribution; no single-AZ clusters allowed", (7, 9)),
    ("Use AWS Systems Manager Session Manager replacing SSH bastion", "technical",
     "SSH bastion hosts require port 22 open to VPN and create security audit findings. Session Manager provides audited shell access to EC2 without SSH keys or open ports. All session activity logged to CloudWatch and S3.",
     "Teleport, HashiCorp Boundary, keep SSH with stricter security groups", "SSH port 22 must be closed on all security groups after Session Manager validated; key rotation eliminated", (5, 7)),
    ("Implement Istio service mesh for zero-trust internal networking", "technical",
     "Internal services communicate over plain HTTP inside the cluster trusting network boundary. Istio injects Envoy sidecars encrypting all service-to-service traffic with mTLS automatically. Policy enforcement defines which services can communicate.",
     "Linkerd, Cilium network policy, app-level TLS, Consul Connect", "Istio adds 5-15% latency overhead — benchmark critical paths before enabling; exclude high-throughput batch services", (9, 11)),
    # AI & ML
    ("Implement prompt injection detection for AI-facing endpoints", "technical",
     "AI document processing endpoint accepts user-provided document content sent directly to Claude API. Malicious documents could manipulate Claude's behaviour via prompt injection. Input sanitisation layer detects and blocks injection patterns before LLM processing.",
     "No sanitisation (trust users), output validation only, sandboxed AI execution", "Sanitisation must not modify legitimate document content; false positive rate must be < 0.1%", (10, 12)),
    ("Build AI evaluation framework for LLM output quality", "technical",
     "No systematic way to measure if Claude API responses are correct for document extraction tasks. Eval framework with 500-document golden set measures extraction accuracy per field. Regression alerts if accuracy drops > 2% after model version changes.",
     "Manual spot-check sampling, A/B testing in production, vendor-provided evals", "Eval must run in CI on every prompt template change; results stored for 12 months for regression analysis", (11, 13)),
    ("Implement human-in-the-loop review for low-confidence AI decisions", "product",
     "Claude API extracts fields with confidence scores but low-confidence extractions go directly to database. Human review queue for documents where any field confidence < 0.85. Reviewer resolves uncertain fields; decisions feed back into future eval set.",
     "Threshold-based rejection (discard low confidence), retrain model, manual review of all documents", "Review queue must not exceed 4-hour SLA; queue depth alerts trigger additional reviewer assignment", (12, 14)),
    ("Use Claude API for automated code review comments", "technical",
     "Code review coverage is inconsistent — security-critical modules reviewed thoroughly, utility code often rubber-stamped. Claude API reviews every PR for security vulnerabilities, missing error handling, and test coverage. Comments appear as GitHub PR review within 90 seconds.",
     "SonarQube static analysis, GitHub Copilot code review, manual review only", "AI review is advisory — cannot block merge; security-critical findings escalated to security lead automatically", (13, 15)),
    ("Implement AI-powered anomaly detection for operational metrics", "technical",
     "Static threshold alerts generate 40% false positives causing alert fatigue and ignored pages. Claude API analyses metric time series context to distinguish genuine anomalies from expected patterns (deployments, traffic spikes). False positive rate drops to 8%.",
     "Prophet forecasting for baselines, AWS DevOps Guru, Datadog Watchdog", "AI anomaly detection supplements but does not replace critical static thresholds; combined alert strategy required", (14, 16)),
    # Additional architectural
    ("Adopt Domain-Driven Design bounded contexts for service decomposition", "architectural",
     "Microservices carved from monolith by technical layer (controllers, services, repositories) rather than business domain. DDD bounded contexts align services with business capabilities. Reduces cross-service coupling from 78 inter-service calls to 23.",
     "Technical layer decomposition, strangler fig without DDD, monolithic frontend with micro-backends", "Domain event catalog must be published before new service boundaries are enforced; 3-sprint transition period", (4, 6)),
    ("Implement API versioning strategy with URL path versioning", "technical",
     "Multiple breaking API changes in the same endpoint have caused 4 partner integration failures. URL path versioning (/api/v1/, /api/v2/) makes version explicit and allows parallel deployment. v1 maintained for 12 months after v2 GA.",
     "Header versioning, query param versioning, GraphQL (no versioning needed), no versioning policy", "New API version requires migration guide published 60 days before v-previous deprecation date", (3, 5)),
    ("Use AWS EFS for shared filesystem across ECS tasks", "technical",
     "Multiple ECS tasks need read/write access to shared configuration files that change at runtime. EFS provides managed NFS with multi-AZ durability and encryption at rest. Mount targets in each AZ for resilient access.",
     "S3 for shared config, DynamoDB for shared state, EBS with manual management", "EFS throughput mode must be elastic to handle burst access patterns; access points enforce directory isolation per service", (8, 10)),
    ("Implement contract testing with Pact", "technical",
     "Integration tests between services run in full environment taking 45 minutes and fail intermittently. Pact consumer-driven contract tests verify service interfaces independently without requiring running dependencies. Run in CI in 3 minutes.",
     "Integration testing only, manual contract documentation, end-to-end tests only", "Consumer must publish Pact to broker before provider CI runs; provider cannot merge if published contracts broken", (7, 9)),
    ("Deploy network load balancer for internal gRPC traffic", "technical",
     "Application load balancer does not support gRPC bidirectional streaming required for real-time order updates. NLB with TCP pass-through allows gRPC streaming while maintaining low latency. Terminates TLS at application layer preserving end-to-end encryption.",
     "ALB with gRPC limitation workaround, Envoy proxy sidecar, service mesh for gRPC", "NLB does not support WAF — gRPC services must implement input validation at application layer", (14, 16)),
]


def generate_synthetic_decisions(rng: random.Random, target_count: int, existing_count: int) -> list[dict]:
    """Generate synthetic decisions from templates to reach target_count total."""
    needed = max(0, target_count - existing_count)
    if needed == 0:
        return []

    result = []
    templates = SYNTHETIC_TEMPLATES.copy()
    # Cycle through templates as many times as needed
    idx = 0
    while len(result) < needed:
        title, dtype, rationale, alternatives, constraints, sprint_range = templates[idx % len(templates)]
        sprint = rng.randint(*sprint_range)
        # Add variation to title if cycling
        cycle = idx // len(templates)
        if cycle > 0:
            suffixes = [
                " — Phase 2", " for new services", " (revised)", " — extended scope",
                " v2", " — all environments", " — production rollout", " — compliance update",
            ]
            title = title + suffixes[cycle % len(suffixes)]
        uid = str(uuid.uuid4())
        conf = round(rng.uniform(0.75, 0.98), 2)
        prd = SPRINT_PRD.get(sprint, "PRD-001: Legacy Modernization")
        branch = FAKE_BRANCHES[(sprint - 1) % len(FAKE_BRANCHES)]
        result.append({
            "uuid": uid,
            "title": title,
            "rationale": rationale,
            "type": dtype,
            "confidence": conf,
            "alternatives": alternatives,
            "constraints": constraints,
            "timestamp": sprint_ts(sprint),
            "project": PROJECT,
            "source": "manual",
            "made_by": rng.choice(MAKERS),
            "context": {
                "source": prd,
                "trigger": title[:100],
                "git_ref": fake_git_ref(uid),
                "branch": branch,
            },
        })
        idx += 1

    return result


# ── Hash chain helpers ────────────────────────────────────────────────────────

def fake_git_ref(seed: str) -> str:
    """Return a deterministic 7-char fake git hash."""
    return hashlib.sha256(seed.encode()).hexdigest()[:7]


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_hash(d: dict) -> str:
    base = {k: v for k, v in d.items() if k not in ("content_hash", "prev_hash")}
    return sha256_hex(json.dumps(base, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


# ── Timestamp helpers ─────────────────────────────────────────────────────────

def sprint_ts(sprint: int, jitter_hours: int = 0) -> str:
    """Return an ISO-8601 timestamp within the given sprint week."""
    sprint_start = EPOCH + timedelta(days=(sprint - 1) * 2.8)
    offset = timedelta(
        hours=random.randint(8, 17),
        minutes=random.randint(0, 59),
        days=jitter_hours // 24 + random.randint(0, 1),
    )
    ts = sprint_start + offset
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def slightly_after(ts_str: str, minutes: int = None) -> str:
    """Return a timestamp slightly after the given one."""
    dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    delta = timedelta(minutes=minutes or random.randint(5, 120))
    return (dt + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Main generator ────────────────────────────────────────────────────────────

def generate(target_dir: Path) -> None:
    rng = random.Random(42)  # deterministic but varied
    smm_dir = target_dir / ".smm"
    smm_dir.mkdir(parents=True, exist_ok=True)

    print(f"Generating demo data in {smm_dir} …")

    # ── 1. Decisions ─────────────────────────────────────────────────────────
    decisions: list[dict] = []
    title_to_uuid: dict[str, str] = {}

    for title, dtype, rationale, alternatives, constraints, sprint in DECISION_CORPUS:
        uid = str(uuid.uuid4())
        title_to_uuid[title] = uid
        ts = sprint_ts(sprint)
        conf = round(rng.uniform(0.75, 0.98), 2)
        prd = SPRINT_PRD.get(sprint, "PRD-001: Legacy Modernization")
        branch = FAKE_BRANCHES[(sprint - 1) % len(FAKE_BRANCHES)]
        record = {
            "uuid": uid,
            "title": title,
            "rationale": rationale,
            "type": dtype,
            "confidence": conf,
            "alternatives": alternatives,
            "constraints": constraints,
            "timestamp": ts,
            "project": PROJECT,
            "source": "manual",
            "made_by": rng.choice(MAKERS),
            "context": {
                "source": prd,
                "trigger": title[:100],
                "git_ref": fake_git_ref(uid),
                "branch": branch,
            },
        }
        decisions.append(record)

    # Pad to 400 with synthetic decisions
    synthetic = generate_synthetic_decisions(rng, target_count=400, existing_count=len(decisions))
    decisions.extend(synthetic)

    # Sort chronologically
    decisions.sort(key=lambda d: d["timestamp"])

    decisions_path = smm_dir / "decisions.jsonl"
    with open(decisions_path, "w", encoding="utf-8") as f:
        for d in decisions:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"  ✓ {len(decisions)} decisions written to decisions.jsonl")

    # ── 2. Contradictions ─────────────────────────────────────────────────────
    contradictions: list[dict] = []
    for (
        title_a, title_b, reason, status, winner_is_a, resolver
    ) in CONTRADICTION_DEFINITIONS:
        uuid_a = title_to_uuid.get(title_a, str(uuid.uuid4()))
        uuid_b = title_to_uuid.get(title_b, str(uuid.uuid4()))

        # Find timestamps for determining which is newer
        ts_a = next((d["timestamp"] for d in decisions if d["uuid"] == uuid_a), sprint_ts(10))
        ts_b = next((d["timestamp"] for d in decisions if d["uuid"] == uuid_b), sprint_ts(5))
        detect_ts = slightly_after(max(ts_a, ts_b), minutes=rng.randint(30, 480))

        entry: dict = {
            "uuid": str(uuid.uuid4()),
            "decision_a": title_a,
            "decision_b": title_b,
            "decision_a_uuid": uuid_a,
            "decision_b_uuid": uuid_b,
            "reason": reason,
            "timestamp": detect_ts,
            "status": status,
            "resolved_winner": None,
            "resolved_by": None,
            "resolved_at": None,
            "ignore_reason": None,
        }

        if status == "resolved":
            winner_title = title_a if winner_is_a else title_b
            resolved_ts = slightly_after(detect_ts, minutes=rng.randint(60, 2880))
            entry["resolved_winner"] = winner_title
            entry["resolved_by"] = resolver
            entry["resolved_at"] = resolved_ts
        elif status == "ignored":
            ignore_ts = slightly_after(detect_ts, minutes=rng.randint(30, 480))
            entry["ignore_reason"] = "Conflict acknowledged — both approaches coexist in different contexts. No action needed."
            entry["resolved_at"] = ignore_ts
            entry["resolved_by"] = resolver

        contradictions.append(entry)

    contradictions_path = smm_dir / "contradictions.jsonl"
    with open(contradictions_path, "w", encoding="utf-8") as f:
        for c in contradictions:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    status_counts = {s: sum(1 for c in contradictions if c["status"] == s) for s in ("resolved", "pending", "ignored", "dismissed")}
    print(f"  ✓ {len(contradictions)} contradictions written — {status_counts}")

    # ── 3. compliance_lineage.jsonl with full hash chain ─────────────────────
    lineage: list[dict] = []
    prev_hash = "GENESIS"

    def append_entry(entry: dict) -> None:
        nonlocal prev_hash
        ch = canonical_hash(entry)
        entry["prev_hash"] = prev_hash
        entry["content_hash"] = ch
        lineage.append(entry)
        prev_hash = ch

    # decision_added events
    for d in decisions:
        append_entry({
            "entry_id": str(uuid.uuid4()),
            "timestamp": d["timestamp"],
            "event_type": "decision_recorded",
            "decision_uuid": d["uuid"],
            "title": d["title"],
            "decision_title": d["title"],
            "rationale": d["rationale"],
            "decision_type": d["type"],
            "confidence": d["confidence"],
            "alternatives": d["alternatives"],
            "constraints": d["constraints"],
            "source": d["source"],
            "made_by": d["made_by"],
            "actor": d["made_by"],
        })

    # contradiction_detected events
    for c in contradictions:
        append_entry({
            "entry_id": str(uuid.uuid4()),
            "timestamp": c["timestamp"],
            "event_type": "contradiction_detected",
            "contradiction_id": c["uuid"],
            "decision_a": c["decision_a"],
            "decision_b": c["decision_b"],
            "decision_a_uuid": c["decision_a_uuid"],
            "decision_b_uuid": c["decision_b_uuid"],
            "explanation": c["reason"],
            "actor": "smm-check",
            "source": "automated",
        })

    # contradiction_resolved / dismissed + decision_superseded
    for c in contradictions:
        if c["status"] == "resolved":
            winner_uuid = c["decision_a_uuid"] if c["resolved_winner"] == c["decision_a"] else c["decision_b_uuid"]
            loser_uuid  = c["decision_b_uuid"] if winner_uuid == c["decision_a_uuid"] else c["decision_a_uuid"]

            append_entry({
                "entry_id": str(uuid.uuid4()),
                "timestamp": c["resolved_at"],
                "event_type": "contradiction_resolved",
                "contradiction_id": c["uuid"],
                "winner": c["resolved_winner"],
                "winner_uuid": winner_uuid,
                "loser_uuid": loser_uuid,
                "rationale": f"Reviewed by {c['resolved_by']} — kept '{c['resolved_winner']}' as the canonical decision.",
                "reviewer": c["resolved_by"],
                "actor": "dashboard",
                "source": "manual",
            })
            append_entry({
                "entry_id": str(uuid.uuid4()),
                "timestamp": slightly_after(c["resolved_at"], minutes=rng.randint(1, 10)),
                "event_type": "decision_superseded",
                "contradiction_id": c["uuid"],
                "superseded_uuid": loser_uuid,
                "superseded_by_uuid": winner_uuid,
                "reviewer": c["resolved_by"],
                "actor": "dashboard",
                "source": "manual",
            })

        elif c["status"] == "dismissed":
            dismiss_ts = slightly_after(c["timestamp"], minutes=rng.randint(30, 360))
            append_entry({
                "entry_id": str(uuid.uuid4()),
                "timestamp": dismiss_ts,
                "event_type": "contradiction_dismissed",
                "contradiction_id": c["uuid"],
                "decision_a": c["decision_a"],
                "decision_b": c["decision_b"],
                "ignore_reason": "Conflict acknowledged — both approaches coexist in different contexts. No action needed.",
                "reviewer": c["resolved_by"] or rng.choice(list(REVIEWERS.keys())),
                "actor": "dashboard",
                "source": "manual",
            })

    # context_injection events — simulate AI agent context lookups across sprints
    injection_agents = [
        ("claude-code", "claude-code", 14),
        ("cursor-agent", "cursor-agent", 8),
        ("claude-code", "claude-code", 7),
        ("lore-hook", "lore-hook", 5),
    ]
    for agent_name, made_by, count in injection_agents:
        for _ in range(count):
            sprint_num = rng.randint(3, 20)
            ts = sprint_ts(sprint_num)
            sampled = rng.sample(decisions, min(count, len(decisions)))
            append_entry({
                "entry_id": str(uuid.uuid4()),
                "timestamp": ts,
                "event_type": "context_injection",
                "agent": agent_name,
                "made_by": made_by,
                "actor": agent_name,
                "source": "automated",
                "decision_count": len(sampled),
                "decisions_surfaced": [d["title"][:60] for d in sampled[:8]],
            })

    # Sort lineage by timestamp before writing
    lineage.sort(key=lambda e: e.get("timestamp", ""))

    # Re-build hash chain after sort
    prev_hash = "GENESIS"
    for entry in lineage:
        base = {k: v for k, v in entry.items() if k not in ("content_hash", "prev_hash")}
        ch = canonical_hash(base)
        entry["prev_hash"] = prev_hash
        entry["content_hash"] = ch
        prev_hash = ch

    lineage_path = smm_dir / "compliance_lineage.jsonl"
    with open(lineage_path, "w", encoding="utf-8") as f:
        for entry in lineage:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"  ✓ {len(lineage)} compliance lineage entries written with SHA-256 hash chain")

    # ── 4. contradiction_index.json ───────────────────────────────────────────
    resolved_ids = [c["uuid"] for c in contradictions if c["status"] == "resolved"]
    ignored_ids  = [c["uuid"] for c in contradictions if c["status"] in ("dismissed", "ignored")]
    index_path = smm_dir / "contradiction_index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump({
            "resolved": resolved_ids,
            "ignored": ignored_ids,
            "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, f, indent=2)
    print(f"  ✓ contradiction_index.json written ({len(resolved_ids)} resolved, {len(ignored_ids)} ignored)")

    # ── 5. config.json ────────────────────────────────────────────────────────
    config_path = smm_dir / "config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump({
            "agent": "claude-code",
            "project": PROJECT,
            "repo_name": "dunder-mifflin-digital",
            "company": "Dunder Mifflin Paper Co",
        }, f, indent=2)

    # ── 6. last_check_timestamp.txt ───────────────────────────────────────────
    ts_path = smm_dir / "last_check_timestamp.txt"
    ts_path.write_text(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    # ── 7. smm.toml (minimal stub for smm to recognise the project) ───────────
    toml_path = target_dir / "smm.toml"
    toml_path.write_text(
        '[project]\nname = "dunder-mifflin-digital"\ndescription = "Dunder Mifflin Paper Co — Digital Transformation"\n'
        'agents = ["claude-code", "cursor-agent"]\n\n'
        '[context]\nproject_type = "enterprise-saas"\nstack = ["Python 3.12", "PostgreSQL", "Kubernetes", "AWS", "Kafka", "React/Next.js"]\n'
    )

    # ── Summary ───────────────────────────────────────────────────────────────
    pending_count  = sum(1 for c in contradictions if c["status"] == "pending")
    resolved_count = sum(1 for c in contradictions if c["status"] == "resolved")
    dismissed_count = sum(1 for c in contradictions if c["status"] == "dismissed")
    print()
    print("=" * 60)
    print("  Dunder Mifflin Digital Transformation — Demo Data Ready")
    print("=" * 60)
    print(f"  Decisions:       {len(decisions)}")
    print(f"  Contradictions:  {len(contradictions)} total")
    print(f"    Resolved:      {resolved_count}")
    print(f"    Pending:       {pending_count}")
    print(f"    Dismissed:     {dismissed_count}")
    print(f"  Lineage entries: {len(lineage)} (SHA-256 chained)")
    print()
    print(f"  Path: {smm_dir}")
    print()
    print("  To launch dashboard:")
    print(f"    cd {target_dir} && smm dashboard")
    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        target = Path(sys.argv[1]).expanduser().resolve()
    else:
        # Default to current working directory (generates into ./. smm/)
        target = Path.cwd()
    target.mkdir(parents=True, exist_ok=True)
    generate(target)
