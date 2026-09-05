# Config
```
# Use a specific project
gcloud config set project <PROJECT_ID>
```

# Compute Engine
```
gcloud compute instances list
gcloud compute instances start <INSTANCE_ID>
gcloud compute instances stop <INSTANCE_ID>
gcloud compute instances delete <INSTANCE_ID>

gcloud compute ssh <INSTANCE_ID> --zone=asia-southeast1-b --tunnel-through-iap
```