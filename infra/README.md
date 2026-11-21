# AWS deployment

Live API: **https://d27aph0gu4g2ie.cloudfront.net**

Interactive docs: **https://d27aph0gu4g2ie.cloudfront.net/docs**

| Method | Public endpoint |
| --- | --- |
| GET | `https://d27aph0gu4g2ie.cloudfront.net/health` |
| GET | `https://d27aph0gu4g2ie.cloudfront.net/model` |
| POST | `https://d27aph0gu4g2ie.cloudfront.net/detect` |
| POST | `https://d27aph0gu4g2ie.cloudfront.net/detect/video/jobs` |
| GET | `https://d27aph0gu4g2ie.cloudfront.net/detect/video/jobs/{job_id}` |

Terraform creates a dedicated `t3.small` EC2 server (2 vCPUs, 2 GiB RAM plus 2 GiB swap), a 24 GiB encrypted
disk, an Elastic IP, a private S3 deployment bucket, an SSM parameter, an instance role,
and a CloudFront distribution with its AWS-provided HTTPS hostname. Existing instances
are not modified. This uses the region's default VPC and a default public subnet.

The EC2 security group only accepts port 80 from CloudFront origin servers. An additional
private origin header prevents other CloudFront distributions from using the server.
SSH and public port 8000 are not exposed; administer via AWS Systems Manager. The
CloudFront-to-origin connection is HTTP inside AWS; viewer connections require HTTPS.
For end-to-end TLS, supply a domain/certificate and configure an HTTPS origin.

## Deploy from Windows

Prerequisites: AWS CLI credentials with permissions for EC2, IAM, S3, SSM, and CloudFront;
Terraform 1.6+; the project's Python virtual environment; and the selected model at
`models/road_damage_best.pt`. Run from the repository root:

```powershell
aws sts get-caller-identity
.\scripts\deploy-aws.ps1
```

The script verifies AWS identity, builds a private bundle, and applies the Terraform
plan. It uses the default AWS credential chain, without copying AWS credentials to the
server. Terraform uploads the bundle to private S3; the server retrieves it using its
instance role. The model remains outside Git. The interim model's redistribution
licence is unclear; obtain permission or use your own weights before distributing it.

Outputs provide `api_url`, `docs_url`, `instance_id`, and `deployment_bucket`. First boot
installs CPU PyTorch and dependencies and can take 10–15 minutes. Check readiness:

```powershell
$api = terraform -chdir=infra output -raw api_url
Invoke-RestMethod "$api/health"
```

The region defaults to `ap-southeast-1`; change `aws_region` using Terraform variables
for another region. The instance uses standard CPU credits to avoid surplus-credit
charges; sustained video processing can slow down when credits run out.

## Public API and website integration

This deployment is intentionally **public, without API keys**, with `Access-Control-Allow-Origin: *`.
Anyone with the URL can consume compute. Nginx limits POST requests to 10/minute per IP
with a burst of 3, limits connections to 10/IP, and caps total request bodies at 51 MiB.
One inference runs at a time; competing requests receive 429 and should retry with
backoff. These limits reduce overload but do not impose an AWS spending cap.

```javascript
const api = "https://d27aph0gu4g2ie.cloudfront.net";
const form = new FormData();
form.append("file", imageFile);
const response = await fetch(`${api}/detect`, { method: "POST", body: form });
if (!response.ok) throw new Error(await response.text());
const detections = await response.json();
```

Do not manually set multipart Content-Type: the browser must provide the boundary.

For videos, use background jobs to avoid CloudFront request timeouts:

```javascript
const form = new FormData();
form.append("file", videoFile);
form.append("frame_stride", "2");
const response = await fetch(`${api}/detect/video/jobs`, { method: "POST", body: form });
if (!response.ok) throw new Error(await response.text());
const job = await response.json();
let result;
do {
  await new Promise(resolve => setTimeout(resolve, 2000));
  const status = await fetch(`${api}${job.status_url}`);
  if (!status.ok) throw new Error(await status.text());
  result = await status.json();
} while (result.status === "processing");
if (result.status === "failed") throw new Error(result.error);
console.log(result.result);
```

The synchronous `/detect/video` endpoint returns 409 on AWS, directing callers to jobs.
Job limits remain 50 MiB, 1,800 decoded frames and 1080p pixel count. Results are kept for
one hour, at most 100 jobs; a restart or deployment clears them. Anyone who knows a job's
unguessable status URL can read its result. Uploads are removed after processing.
This is a single-server deployment, not a durable multi-server queue or high-availability
service. Add durable job storage and a queue before scaling beyond one worker.

## Operations and costs

The Singapore Linux `t3.small` costs about $19/month at 730 hours, plus EBS, public IPv4,
CloudFront requests/traffic and S3. Expect roughly $25–30/month before significant traffic;
actual charges depend on current prices and use. Free Tier credits may offset charges
until exhausted or expired. This account's Free plan rejected `t3.medium`; `t3.small`
is an eligible type. Upgrade the instance only after checking your account's allowed types.

Use AWS CLI SSM Run Command to inspect the server without SSH:

```powershell
$id = terraform -chdir=infra output -raw instance_id
aws ssm send-command --instance-ids $id --document-name AWS-RunShellScript --parameters 'commands=["systemctl status rdd-bootstrap rdd-api --no-pager","journalctl -u rdd-api -n 40 --no-pager"]'
```

For application updates, rerun the deployment script. A changed bundle replaces the
server, briefly interrupting service and clearing jobs; the HTTPS hostname stays the
same. Keep `infra/terraform.tfstate` safe: it tracks resources and contains the private
origin token. State, plans, binaries, credentials, and deployment bundles are gitignored.
Use a protected remote Terraform backend before sharing infrastructure administration.

To remove this deployment and stop its ongoing compute charges:

```powershell
terraform -chdir=infra destroy
```

This also deletes the dedicated deployment bucket and its code/model bundle. It does not
delete local models or your other AWS instances. CloudFront deletion can take several minutes.
