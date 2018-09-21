#!/usr/bin/env python3
"""
Clean docker images

This serves as a substitute for Kubernetes ImageGC
which has thresholds that are not sufficiently configurable on GKE
at this time.
"""

import os
import time
import logging

import docker
import requests


logging.basicConfig(format='%(asctime)s %(message)s',
                    level=logging.INFO)


def get_used_percent(path):
    """
    Return disk usage as a percentage

    (100 is full, 0 is empty)

    Calculated by blocks or inodes,
    which ever reports as the most full.
    """
    stat = os.statvfs(path)
    inodes_avail = stat.f_favail / stat.f_files
    blocks_avail = stat.f_bavail / stat.f_blocks
    return 100 * (1 - min(blocks_avail, inodes_avail))


def image_key(image):
    """Sort key for images

    Prefers untagged images, sorted by size
    """
    return (not image.tags, image.attrs['Size'])


def get_docker_images(client):
    """Return list of docker images, sorted by size

    Untagged images will come first
    """
    images = client.images.list(all=True)
    images.sort(key=image_key, reverse=True)
    return images


def cordon(kube, node):
    """cordon a kubernetes node"""
    kube.patch_node(
        node,
        {
            "spec": {
                "unschedulable": True,
            },
        },
    )


def uncordon(kube, node):
    """uncordon a kubernetes node"""
    kube.patch_node(
        node,
        {
            "spec": {
                "unschedulable": False,
            },
        },
    )


def main():
    node = os.getenv('NODE_NAME')
    if node:
        import kubernetes.config
        import kubernetes.client
        try:
            kubernetes.config.load_incluster_config()
        except Exception:
            kubernetes.config.load_kube_config()
        kube = kubernetes.client.CoreV1Api()
        # verify that we can talk to the node
        kube.read_node(node)

    path_to_check = os.getenv('PATH_TO_CHECK', '/var/lib/docker')
    delay = float(os.getenv('IMAGE_GC_DELAY', '1'))
    gc_low = float(os.getenv('IMAGE_GC_THRESHOLD_LOW', '60'))
    gc_high = float(os.getenv('IMAGE_GC_THRESHOLD_HIGH', '80'))
    client = docker.from_env(version='auto')
    images = get_docker_images(client)

    logging.info(f'Pruning docker images when {path_to_check} has {gc_high}% inodes or blocks used')

    while True:
        used = get_used_percent(path_to_check)
        logging.info(f'{used:.1f}% used')
        if used < gc_high:
            # Do nothing! We have enough space
            pass
        else:
            images = get_docker_images(client)
            if not images:
                logging.info(f'No images to delete')
            else:
                logging.info(f'{len(images)} images available to prune')

            start = time.perf_counter()
            images_before = len(images)

            if node:
                logging.info(f"Cordoning node {node}")
                cordon(kube, node)

            deleted = 0

            while images and get_used_percent(path_to_check) > gc_low:
                # Ensure the node is still cordoned
                if node:
                    logging.info(f"Cordoning node {node}")
                    cordon(kube, node)
                # Remove biggest image
                image = images.pop(0)
                if image.tags:
                    # does it have a name, e.g. jupyter/base-notebook:12345
                    name = image.tags[0]
                else:
                    # no name, use id
                    name = image.id
                gb = image.attrs['Size'] / (2**30)
                logging.info(f'Removing {name} (size={gb:.2f}GB)')
                try:
                    client.images.remove(image=image.id, force=True)
                    logging.info(f'Removed {name}')
                    # Delay between deletions.
                    # A sleep here avoids monopolizing the Docker API with deletions.
                    time.sleep(delay)
                except docker.errors.APIError as e:
                    if e.status_code == 409:
                        # This means the image can not be removed right now
                        logging.info(f'Failed to remove {name}, skipping this image')
                        logging.info(str(e))
                    elif e.status_code == 404:
                        logging.info(f'{name} not found, probably already deleted')
                    else:
                        raise
                except requests.exceptions.ReadTimeout:
                    logging.warning(f'Timeout removing {name}')
                    # Delay longer after a timeout, which indicates that Docker is overworked
                    time.sleep(max(delay, 30))
                else:
                    deleted += 1

            if node:
                logging.info(f"Uncordoning node {node}")
                uncordon(kube, node)

            # log what we did and how long it took
            duration = time.perf_counter() - start
            images_deleted = images_before - len(images)
            logging.info(f"Deleted {images_deleted} images in {int(duration)} seconds")

        time.sleep(60)


if __name__ == '__main__':
    main()
