# AWS Scripting Tools for Zabbix & Metrics Monitoring 🚀☁️

Este repositorio contiene una colección de scripts en **Python** diseñados para monitorear y extraer estadísticas de diversos servicios de **AWS**. Muchos de ellos están pensados para integrarse con **Zabbix**, permitiéndote centralizar la monitorización de recursos como Auto Scaling, CloudWatch, EBS, EC2, Lambda y RDS.

> **Nota:** Asegúrate de tener configuradas tus credenciales de AWS y contar con las librerías necesarias (por ejemplo, [boto3](https://pypi.org/project/boto3/)) para que estos scripts funcionen correctamente.

---

## 📂 Contenido del Repositorio

| Script                           | Descripción                                                                                     |
| -------------------------------- | ----------------------------------------------------------------------------------------------- |
| `autoscaling_zabbix.py`          | Obtiene métricas de grupos de Auto Scaling y las envía a Zabbix.                                  |
| `cloudwatch_zabbix.py`           | Recupera métricas personalizadas desde CloudWatch para su integración con Zabbix.                 |
| `ebs_stats.py`                   | Extrae estadísticas de volúmenes EBS (IOPS, throughput, latencia, etc.).                         |
| `ec2_stats.py`                   | Recopila estadísticas y métricas de instancias EC2 (uso de CPU, tráfico de red, etc.).             |
| `lambda_zabbix.py`               | Monitorea funciones AWS Lambda, extrayendo métricas como invocaciones, errores y duración.         |
| `rds_stats.py`                   | Obtiene estadísticas de instancias RDS (uso de CPU, latencia de lectura/escritura, conexiones, etc.)|
| `cleanup_amis_snapshots.py` | Elimina AMIs y sus snapshots asociados en la región especificada.                                 |
| `s3_cleanup_versions.py`    | Elimina todas las versiones de objetos y marcadores de eliminación en un bucket de S3 específico.|
---

## ⚙️ Requisitos

- **Python 3.x**
- **[boto3](https://pypi.org/project/boto3/)** (SDK de AWS para Python)
- Credenciales de AWS configuradas (por ejemplo, mediante variables de entorno o el AWS CLI)
- Acceso a un servidor Zabbix (para los scripts que integran datos en Zabbix)

---

## 🚀 Instalación

1. **Clonar el repositorio:**
   ```bash
   git clone https://github.com/PedroFernandz/aws-scripting.git
   cd aws-scripting


## 📂 Contenido del Repositorio y Ejemplos de Uso

### `autoscaling_zabbix.py` 🔄  
**Descripción:**  
Obtiene métricas de grupos de Auto Scaling y las envía a Zabbix.  

**Ejemplo de uso:**  
```bash
python autoscaling_zabbix.py --zabbix-server 192.168.1.100 --group-name my-auto-scaling-group
```


### `cloudwatch_zabbix.py` 🔄  
**Descripción:**  
Recupera métricas personalizadas desde CloudWatch para su integración con Zabbix.

**Ejemplo de uso:**  
```bash
python cloudwatch_zabbix.py --namespace "AWS/EC2" --metric "CPUUtilization" --dimension "InstanceId=i-0123456789abcdef0" --zabbix-server 192.168.1.100
```

### `ebs_stats.py` 🔄  
**Descripción:**  
Extrae estadísticas de volúmenes EBS, como IOPS, throughput y latencia..

**Ejemplo de uso:**  
```bash
python ebs_stats.py --volume-id vol-1234567890abcdef0
```

### `ec2_stats.py` 🔄  
**Descripción:**  
Recopila estadísticas y métricas de instancias EC2, como uso de CPU y tráfico de red.

**Ejemplo de uso:**  
```bash
python ec2_stats.py --instance-id i-0123456789abcdef0
```
### `lambda_zabbix.py` 🔄  
**Descripción:**  
Monitorea funciones AWS Lambda, extrayendo métricas como invocaciones, errores y duración.

**Ejemplo de uso:**  
```bash
python lambda_zabbix.py --function-name my_lambda_function --zabbix-server 192.168.1.100

```
### `rds_stats.py` 🔄  
**Descripción:**  
Obtiene estadísticas de instancias RDS, como uso de CPU, latencia de lectura/escritura y conexiones activas.

**Ejemplo de uso:**  
```bash
python rds_stats.py --db-instance-identifier mydbinstance

```

### `cleanup_amis_snapshots.py` 🧹

**Descripción:**
Este script se conecta a tu cuenta de AWS en la región especificada y elimina las Amazon Machine Images (AMIs) y sus snapshots asociados. Está diseñado para ayudar a limpiar AMIs no deseadas y liberar espacio de almacenamiento.

**Uso:**
1. Asegúrate de tener configuradas tus credenciales de AWS y de que el usuario tenga permisos para gestionar AMIs y snapshots.
2. Modifica el script para especificar la región deseada cambiando el valor de la variable `REGION`.
3. Ejecuta el script:
   ```bash
   python cleanup_amis_snapshots.py
   ```

---

### `s3_cleanup_versions.py` 🗑️

**Descripción:**
Este script se conecta a tu bucket de Amazon S3 y elimina todas las versiones de objetos y los marcadores de eliminación presentes. Es útil para limpiar buckets con versionado habilitado y liberar espacio de almacenamiento.

**Uso:**
1. Asegúrate de tener configuradas tus credenciales de AWS y de que el usuario tenga permisos para gestionar objetos en S3.
2. Modifica el script para especificar el nombre de tu bucket y la región deseada cambiando los valores de las variables `bucket_name` y `region_name`.
3. Ejecuta el script:
   ```bash
   python s3_cleanup_versions.py
   ```



